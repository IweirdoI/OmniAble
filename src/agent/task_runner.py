"""通用任务执行器（TASK-042）：门控、统一动作目录分发与运行记录关联。

门控优先级（任何一步不满足即停 waiting_user，不启动被拦截的动作）：
  1. 任务暂停/终态 → 拒绝（store 层守卫）
  2. assist 模式 → 不执行，返回提示文本（提示协助口径）
  3. unknown 步骤 → 拒绝；须用户核对（review）回 pending 后显式重试
  4. external 步骤 → 必须已有确认且参数指纹一致；内容变化即作废确认
  5. 互斥写动作（mutates）在任务通道暂不开放（走聊天确认通道）

执行走 TASK-021 目录 dispatch_action 或确定性本地步骤（local:*，纯本
地计算/留痕，无外部业务写入）；每次执行经 RunStore 记录（幂等键含步
骤 updated_at，核对重试产生新键），run_record_id 回填到步骤。
"""

from __future__ import annotations

import hashlib
import json
import os

from src.agent.task_store import TaskStoreError
from typing import Any

GUIDE = {
    "assist": "提示协助模式：以下步骤由你亲自操作，Agent 提供说明。",
}


def _fingerprint(args: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(args, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


class TaskStepError(RuntimeError):
    """步骤被门控拦截或参数非法；message 面向用户可读。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TaskRunner:
    """对 store 中任务的步骤执行门控与分发；无执行线程，全部显式触发。"""

    def __init__(self, store, run_store=None) -> None:
        self.store = store
        self.run_store = run_store

    # -- 入口 ---------------------------------------------------------------

    def run_step(self, user_id: str, task_id: str, step_id: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task is None:
            raise TaskStepError("task_not_found", "任务不存在。")
        try:
            return self._run_step_checked(user_id, task_id, task, step_id)
        except TaskStoreError as exc:
            raise TaskStepError("task_not_runnable", str(exc)) from exc

    def _run_step_checked(self, user_id: str, task_id: str,
                          task: dict[str, Any], step_id: str) -> dict[str, Any]:
        if task["mode"] == "assist":
            raise TaskStepError("assist_mode", GUIDE["assist"] + self._plan_text(task))
        step = self.store.get_step(user_id, task_id, step_id)
        if step is None:
            raise TaskStepError("step_not_found", "步骤不存在。")
        if step["status"] == "unknown":
            self.store.set_task_status(user_id, task_id, "waiting_user",
                                       detail="结果未知，等待核对")
            raise TaskStepError(
                "review_required", "该步骤结果未知，请先核对再显式重试。")
        if step["status"] != "pending":
            raise TaskStepError(
                "step_not_runnable", f"步骤当前状态 {step['status']}，不能执行。")
        if step["external"]:
            try:
                self._ensure_external_confirmed(step, task)
            except TaskStepError as exc:
                # 外发被拦：任务停在等待用户（异常/外发前停下确认）
                self.store.set_task_status(user_id, task_id, "waiting_user",
                                           detail=exc.message)
                raise
        return self._execute(user_id, task_id, step)

    def confirm_external(self, user_id: str, task_id: str,
                         step_id: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task is None:
            raise TaskStepError("task_not_found", "任务不存在。")
        step = self.store.get_step(user_id, task_id, step_id)
        if step is None:
            raise TaskStepError("step_not_found", "步骤不存在。")
        if not step["external"]:
            raise TaskStepError("not_external", "该步骤不是外发步骤。")
        self.store.confirm_step(user_id, task_id, step_id,
                                scope=_fingerprint(step["args"]))
        updated = self.store.get_step(user_id, task_id, step_id)
        assert updated is not None
        return updated

    def review_unknown(self, user_id: str, task_id: str,
                       step_id: str) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        if task is None:
            raise TaskStepError("task_not_found", "任务不存在。")
        updated = self.store.confirm_step(user_id, task_id, step_id, review=True)
        if updated is None:
            raise TaskStepError("step_not_found", "步骤不存在。")
        return updated

    # -- 门控 ---------------------------------------------------------------

    def _ensure_external_confirmed(self, step: dict[str, Any],
                                   task: dict[str, Any]) -> None:
        if not step["confirmed_at"]:
            raise TaskStepError(
                "external_confirmation_required",
                "外发/正式提交步骤需要单独确认；已停在等待用户。")
        if step["confirmed_scope"] != _fingerprint(step["args"]):
            # 参数/内容在确认后发生变化：作废确认（重放不能继承上次批准）
            self.store.reset_external_confirm(task["user_id"], task["id"],
                                              step["step_id"])
            raise TaskStepError(
                "confirmation_stale", "步骤内容已变化，原确认作废，请重新确认。")

    # -- 执行 ---------------------------------------------------------------

    def _execute(self, user_id: str, task_id: str,
                 step: dict[str, Any]) -> dict[str, Any]:
        task = self.store.get_task(user_id, task_id)
        assert task is not None
        running = self.store.mark_step_running(user_id, task_id,
                                               step["step_id"])
        if running is None:
            raise TaskStepError("step_not_found", "步骤不存在。")
        run_record_id = self._begin_run_record(task, step)
        action = step["action"]
        try:
            if action.startswith("local:"):
                result = _run_local(action[len("local:"):], task, step)
                ok, error = True, None
            else:
                ok, result, error = _dispatch_catalog(action, step["args"])
        except TaskStepError:
            self.store.finish_step(user_id, task_id, step["step_id"],
                                   status="failed", error_code="step_failed",
                                   run_record_id=run_record_id)
            self._finish_run_record(user_id, run_record_id, "failed",
                                    "step_failed")
            raise
        status = "done" if ok else "failed"
        if ok:
            _merge_draft(self.store, user_id, task_id, action, result, step)
        step = self.store.finish_step(
            user_id, task_id, step["step_id"], status=status, result=result,
            error_code=error, run_record_id=run_record_id)
        assert step is not None
        self._finish_run_record(user_id, run_record_id,
                                "completed" if ok else "failed", error)
        return step

    def _begin_run_record(self, task: dict[str, Any],
                          step: dict[str, Any]) -> str | None:
        """步骤执行经 RunStore 记录（TASK-022）；幂等键含步骤 updated_at，
        核对重试产生新键。"""
        if self.run_store is None:
            return None
        try:
            record, dup = self.run_store.begin_run(
                task["user_id"], "task-" + step["step_id"][:12],
                route="task",
                idempotency_key=f"task:{task['id']}:{step['step_id']}:"
                                f"{step['updated_at']}")
            if not dup:
                self.run_store.mark_running(task["user_id"], record["id"])
            return record["id"]
        except Exception:  # noqa: BLE001 —— 记录失败不阻断执行，如实降级
            return None

    def _finish_run_record(self, user_id: str, run_record_id: str | None,
                           status: str, error: str | None) -> None:
        if self.run_store is None or run_record_id is None:
            return
        try:
            self.run_store.finish_run(user_id, run_record_id, status,
                                      stop_reason=error, steps=[])
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _plan_text(task: dict[str, Any]) -> str:
        lines = [f"{i + 1}. {s['name']}（{s['status']}）"
                 for i, s in enumerate(task["steps"])]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 目录动作分发（同调度器 default_runner 的权限口径）
# ---------------------------------------------------------------------------

def _dispatch_catalog(action_name: str, args: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    """任务通道的动作权限口径：只读本地动作直接可用；联网/终端/记忆按
    开关；写动作一律拒绝（走聊天确认通道，任务通道不代替确认）。"""
    from src.agent.actions import ActionContext, get_action

    action = get_action(action_name)
    if action is None:
        return False, None, f"动作不存在：{action_name}"
    gated = {
        "web_fetch": os.getenv("AGENT_ENABLE_WEB_TOOL", "0") == "1",
        "exec_terminal": os.getenv("AGENT_ENABLE_EXEC_TOOL", "0") == "1",
        "search_memory": False,
    }
    if action_name in gated:
        if not gated[action_name]:
            return False, None, f"动作当前不可用（开关未开）：{action_name}"
    if action.mutates:
        return False, None, "变更类动作需在聊天中经确认卡执行，任务通道不代替确认"
    ctx = ActionContext(tools_enabled=True)
    ok, content, error = _dispatch_action(action_name, args, ctx)
    return ok, (content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)), error


def _dispatch_action(action_name: str, args: dict[str, Any], ctx):
    from src.agent.actions import dispatch_action
    return dispatch_action(action_name, args, ctx)


# ---------------------------------------------------------------------------
# 确定性本地步骤（纯本地计算/留痕；不产生外部业务写入）
# ---------------------------------------------------------------------------

def _run_local(name: str, task: dict[str, Any], step: dict[str, Any]) -> str:
    args = step["args"]
    if name == "schedule_draft":
        text = str(args.get("text") or "")
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            items.append({"raw": line, "time": _extract_time(line)})
        return json.dumps({"schedule": items}, ensure_ascii=False)
    if name == "parse_fields":
        text = str(args.get("text") or "")
        fields = {}
        for line in text.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        return json.dumps({"fields": fields}, ensure_ascii=False)
    if name == "verify_fields":
        fields = (task.get("draft") or {}).get("fields") or {}
        expected = args.get("expected") or {}
        diffs = [
            {"field": key, "source": fields.get(key), "expected": expected[key]}
            for key in expected if fields.get(key) != expected[key]
        ]
        return json.dumps(
            {"verify": {"result": "pass" if not diffs else "diff",
                        "diffs": diffs}},
            ensure_ascii=False)
    if name == "record_submission":
        return json.dumps({
            "outbox": {
                "recorded_at": "local",
                "note": "本地留痕：未接外部系统，正式外发需另行接入",
                "summary": str(args.get("summary") or ""),
            }
        }, ensure_ascii=False)
    raise TaskStepError("unknown_local_step", f"未知本地步骤：{name}")


def _extract_time(line: str) -> str | None:
    import re
    match = re.search(r"(\d{1,2}[:：]\d{2})", line)
    return match.group(1).replace("：", ":") if match else None


def _merge_draft(store, user_id: str, task_id: str, action: str,
                 result: str | None, step: dict[str, Any]) -> None:
    """把步骤产出合并进任务草稿（display 口径；失败不写）。"""
    if not result:
        return
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return
    task = store.get_task(user_id, task_id)
    if task is None:
        return
    draft = dict(task.get("draft") or {})
    for key in ("schedule", "fields", "verify", "outbox"):
        if key in payload:
            draft[key] = payload[key]
    store.update_draft(user_id, task_id, draft)


# ---------------------------------------------------------------------------
# 样例流模板（两类任务走同一状态机与接管机制）
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, dict[str, Any]] = {
    "life_schedule": {
        "goal_hint": "生活通知整理日程草稿",
        "steps": [
            {"step_id": "collect", "name": "获取当前时间基准",
             "action": "get_current_datetime", "arg_keys": []},
            {"step_id": "organize", "name": "整理日程草稿",
             "action": "local:schedule_draft", "arg_keys": ["text"]},
            {"step_id": "push", "name": "写入日历（外发）",
             "action": "local:record_submission", "arg_keys": ["summary"],
             "external": True},
        ],
    },
    "work_verify": {
        "goal_hint": "工作资料字段核验",
        "steps": [
            {"step_id": "parse", "name": "提取资料字段",
             "action": "local:parse_fields", "arg_keys": ["text"]},
            {"step_id": "verify", "name": "核对字段与预期",
             "action": "local:verify_fields", "arg_keys": ["expected"]},
            {"step_id": "submit", "name": "提交核验结果（外发）",
             "action": "local:record_submission", "arg_keys": ["summary"],
             "external": True},
        ],
    },
}


def build_plan(kind: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """按样例模板生成计划；payload 注入各步骤参数（来源文本/预期字段等）。"""
    template = TEMPLATES.get(kind)
    if template is None:
        raise TaskStepError("unknown_kind", f"未知任务类型：{kind}")
    steps = []
    for item in template["steps"]:
        step = dict(item)
        arg_keys = list(step.pop("arg_keys", []))
        extra = (payload.get("step_args") or {}).get(step["step_id"], {})
        args = {key: payload[key] for key in arg_keys if payload.get(key) is not None}
        args.update({k: v for k, v in extra.items() if v is not None})
        step["args"] = args
        steps.append(step)
    return steps
