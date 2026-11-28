"""通用任务持久层（TASK-042 / Issue #62，DOC-002 任务契约首轮落地）。

与 SessionStore / RunStore / SchedulerTaskStore 同风格：SQLite 单文件
（默认 data/agent_tasks.db，环境变量 OS_AGENT_TASKS_DB 覆盖）、RLock +
WAL、严格用户隔离。只做持久化与受控状态迁移，执行门控在 task_runner。

任务状态机（对齐 DOC-002"任务进度"契约）：
  ready → running ⇄ waiting_user → ready | completed
  任意非终态 → suspended（用户暂停）→ 原状态恢复
  遗留 running 在重启后 → suspended + 未决步骤 unknown（不冒充成功）
步骤状态：pending / running / done / failed / unknown / skipped / blocked
  - unknown：结果未知（外部已受理但未确认、或中断）——禁止自动重发，
    须经用户核对（review）后回到 pending 才可重试
  - external 步骤（外发/正式提交类）必须先经单独确认（绑定步骤与参数
    版本），否则 runner 拒绝执行并停在 waiting_user
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

DEFAULT_DB_PATH = "data/agent_tasks.db"

TASK_STATUSES = ("ready", "running", "waiting_user", "suspended",
                 "completed", "failed")
# 允许迁移到的目标状态（受控迁移；来源校验在 runner/store 各守卫内）
STEP_STATUSES = ("pending", "running", "done", "failed", "unknown",
                 "skipped", "blocked")

_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    goal TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    draft_json TEXT NOT NULL DEFAULT '{}',
    scope_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS task_steps (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    action TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    external INTEGER NOT NULL DEFAULT 0,
    confirmed_at TEXT,
    confirmed_scope TEXT,
    run_record_id TEXT,
    result TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, step_id)
);
"""


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


class TaskStoreError(RuntimeError):
    pass


class TaskStore:
    """通用任务与步骤持久层；线程安全（同 SessionStore 锁模式）。"""

    def __init__(self, db_path: str | Path | None = None, *,
                 project_root: str | None = None) -> None:
        self.path = Path(db_path or os.getenv("OS_AGENT_TASKS_DB") or DEFAULT_DB_PATH)
        if project_root and not self.path.is_absolute():
            self.path = Path(project_root) / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 任务创建与读取 -----------------------------------------------------

    def create_task(self, user_id: str, *, kind: str, goal: str, mode: str,
                    steps: list[dict[str, Any]], draft: dict[str, Any] | None = None,
                    scope: list[str] | None = None,
                    now: str | None = None) -> dict[str, Any]:
        """创建任务（计划步骤随创建写入）；目标与模式必填。"""
        if not str(user_id or "").strip():
            raise TaskStoreError("任务缺少归属用户身份")
        if mode not in ("assist", "step", "delegate"):
            raise TaskStoreError(f"未知协助程度：{mode}")
        if not str(goal or "").strip():
            raise TaskStoreError("任务目标不能为空")
        if not steps:
            raise TaskStoreError("任务计划不能为空")
        ts = now or _now()
        task_id = uuid4().hex
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks(id, user_id, kind, goal, mode, status,"
                " detail, draft_json, scope_json, created_at, updated_at)"
                " VALUES(:id, :u, :kind, :goal, :mode, 'ready', NULL,"
                " :draft, :scope, :ts, :ts)",
                {"id": task_id, "u": user_id, "kind": kind, "goal": goal,
                 "mode": mode, "draft": json.dumps(draft or {}, ensure_ascii=False),
                 "scope": json.dumps(scope or [], ensure_ascii=False), "ts": ts})
            for seq, step in enumerate(steps):
                self._conn.execute(
                    "INSERT INTO task_steps(task_id, step_id, seq, name, action,"
                    " args_json, status, external, updated_at)"
                    " VALUES(:t, :sid, :seq, :name, :action, :args, :status,"
                    " :ext, :ts)",
                    {"t": task_id, "sid": str(step["step_id"]), "seq": seq,
                     "name": str(step["name"]), "action": str(step["action"]),
                     "args": json.dumps(step.get("args") or {}, ensure_ascii=False),
                     "status": "pending",
                     "ext": 1 if step.get("external") else 0, "ts": ts})
        return self.get_task(user_id, task_id)  # type: ignore[return-value]

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any] | None:
        """任务详情（含步骤，按 seq 排序）；跨用户/不存在 → None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = :t AND user_id = :u",
                {"t": task_id, "u": user_id}).fetchone()
            if row is None:
                return None
            steps = self._conn.execute(
                "SELECT * FROM task_steps WHERE task_id = :t ORDER BY seq ASC",
                {"t": task_id}).fetchall()
        task = dict(row)
        task["draft"] = json.loads(task.pop("draft_json") or "{}")
        task["scope"] = json.loads(task.pop("scope_json") or "[]")
        task["steps"] = [self._step_out(dict(s)) for s in steps]
        return task

    def list_tasks(self, user_id: str, *, offset: int = 0,
                   limit: int = 50) -> dict[str, Any]:
        """按更新时间倒序分页（含步骤摘要计数）。"""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE user_id = :u",
                {"u": user_id}).fetchone()["n"]
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE user_id = :u"
                " ORDER BY updated_at DESC LIMIT :limit OFFSET :offset",
                {"u": user_id, "limit": limit, "offset": offset}).fetchall()
        items = []
        for row in rows:
            task = dict(row)
            task["draft"] = json.loads(task.pop("draft_json") or "{}")
            task["scope"] = json.loads(task.pop("scope_json") or "[]")
            with self._lock:
                counts = self._conn.execute(
                    "SELECT status, COUNT(*) AS n FROM task_steps"
                    " WHERE task_id = :t GROUP BY status",
                    {"t": task["id"]}).fetchall()
            task["step_counts"] = {c["status"]: c["n"] for c in counts}
            items.append(task)
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    # -- 受控状态迁移 -------------------------------------------------------

    def set_task_status(self, user_id: str, task_id: str, status: str, *,
                        detail: str | None = None, mode: str | None = None,
                        expect: tuple[str, ...] | None = None) -> dict[str, Any] | None:
        """迁移任务状态；expect 给定时校验来源状态（乐观守卫）。"""
        if status not in TASK_STATUSES:
            raise TaskStoreError(f"未知任务状态：{status}")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE id = :t AND user_id = :u",
                {"t": task_id, "u": user_id}).fetchone()
            if row is None:
                return None
            if expect and row["status"] not in expect:
                raise TaskStoreError(
                    f"状态迁移不允许：{row['status']} → {status}")
            self._conn.execute(
                "UPDATE tasks SET status = :s,"
                " detail = COALESCE(:d, detail),"
                " mode = COALESCE(:m, mode), updated_at = :ts"
                " WHERE id = :t AND user_id = :u",
                {"s": status, "d": detail, "m": mode, "t": task_id,
                 "u": user_id, "ts": _now()})
        return self.get_task(user_id, task_id)

    def update_draft(self, user_id: str, task_id: str,
                     draft: dict[str, Any]) -> dict[str, Any] | None:
        """补充信息/草稿更新（用户可编辑内容与说明）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET draft_json = :d, updated_at = :ts"
                " WHERE id = :t AND user_id = :u",
                {"d": json.dumps(draft, ensure_ascii=False), "t": task_id,
                 "u": user_id, "ts": _now()})
            if cur.rowcount == 0:
                return None
        return self.get_task(user_id, task_id)

    def takeover(self, user_id: str, task_id: str) -> dict[str, Any] | None:
        """接管：协助程度切 assist、状态转 waiting_user；不可对已完成任务接管。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE id = :t AND user_id = :u",
                {"t": task_id, "u": user_id}).fetchone()
            if row is None:
                return None
            if row["status"] in ("completed", "failed"):
                raise TaskStoreError("任务已终态，无需接管")
            self._conn.execute(
                "UPDATE tasks SET mode = 'assist', status = 'waiting_user',"
                " detail = :d, updated_at = :ts WHERE id = :t AND user_id = :u",
                {"d": "用户已接管，等待新的指示", "t": task_id, "u": user_id,
                 "ts": _now()})
        return self.get_task(user_id, task_id)

    # -- 步骤操作 -----------------------------------------------------------

    def get_step(self, user_id: str, task_id: str,
                 step_id: str) -> dict[str, Any] | None:
        task = self.get_task(user_id, task_id)
        if task is None:
            return None
        for step in task["steps"]:
            if step["step_id"] == step_id:
                return step
        return None

    def mark_step_running(self, user_id: str, task_id: str,
                          step_id: str) -> dict[str, Any] | None:
        """步骤置 running；任务同步置 running（乐观守卫：任务不得在
        suspended/completed/failed 下执行）。"""
        with self._lock, self._conn:
            task = self._conn.execute(
                "SELECT status FROM tasks WHERE id = :t AND user_id = :u",
                {"t": task_id, "u": user_id}).fetchone()
            if task is None:
                return None
            if task["status"] in ("suspended", "completed", "failed"):
                raise TaskStoreError(f"任务状态 {task['status']} 不允许执行步骤")
            self._conn.execute(
                "UPDATE task_steps SET status = 'running', updated_at = :ts"
                " WHERE task_id = :t AND step_id = :s",
                {"t": task_id, "s": step_id, "ts": _now()})
            self._conn.execute(
                "UPDATE tasks SET status = 'running', updated_at = :ts"
                " WHERE id = :t AND user_id = :u",
                {"t": task_id, "u": user_id, "ts": _now()})
        return self.get_step(user_id, task_id, step_id)

    def finish_step(self, user_id: str, task_id: str, step_id: str, *,
                    status: str, result: str | None = None,
                    error_code: str | None = None,
                    run_record_id: str | None = None) -> dict[str, Any] | None:
        """步骤终态（done/failed/unknown/skipped）；未知/失败时任务停
        waiting_user（异常停下），成功则回到 ready 并推进说明。"""
        if status not in ("done", "failed", "unknown", "skipped"):
            raise TaskStoreError(f"非法步骤终态：{status}")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE task_steps SET status = :status, result = :result,"
                " error_code = :error,"
                " run_record_id = COALESCE(:rid, run_record_id),"
                " updated_at = :ts WHERE task_id = :task_id AND step_id = :step_id",
                {"status": status, "result": result, "error": error_code,
                 "rid": run_record_id, "task_id": task_id, "step_id": step_id,
                 "ts": _now()})
            remaining = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM task_steps"
                " WHERE task_id = :t GROUP BY status",
                {"t": task_id}).fetchall()
            counts = {r["status"]: r["n"] for r in remaining}
        if counts.get("done", 0) == sum(counts.values()):
            task_status, detail = "completed", "全部步骤完成"
        elif status in ("failed", "unknown"):
            task_status = "waiting_user"
            detail = ("步骤结果未知，等待核对" if status == "unknown"
                      else f"步骤失败：{error_code or '未说明原因'}")
        else:
            task_status, detail = "ready", None
        self.set_task_status(user_id, task_id, task_status, detail=detail)
        return self.get_step(user_id, task_id, step_id)

    def confirm_step(self, user_id: str, task_id: str, step_id: str, *,
                     scope: str = "", review: bool = False) -> dict[str, Any] | None:
        """确认外发步骤（绑定授权范围与时间）；review=True 表示用户对
        unknown 步骤完成核对——回到 pending 允许重试（不自动重发，仍需
        显式触发）。"""
        with self._lock, self._conn:
            step = self._conn.execute(
                "SELECT s.* FROM task_steps s JOIN tasks t ON t.id = s.task_id"
                " WHERE s.task_id = :t AND s.step_id = :s AND t.user_id = :u",
                {"t": task_id, "s": step_id, "u": user_id}).fetchone()
            if step is None:
                return None
            if review:
                if step["status"] != "unknown":
                    raise TaskStoreError("只有结果未知的步骤需要核对")
                self._conn.execute(
                    "UPDATE task_steps SET status = 'pending', updated_at = :ts"
                    " WHERE task_id = :t AND step_id = :s",
                    {"t": task_id, "s": step_id, "ts": _now()})
                self.set_task_status(user_id, task_id, "waiting_user",
                                     detail="步骤已核对，可显式重试")
            else:
                if not step["external"]:
                    raise TaskStoreError("该步骤不是外发步骤，无需单独确认")
                self._conn.execute(
                    "UPDATE task_steps SET confirmed_at = :ts,"
                    " confirmed_scope = :scope, updated_at = :ts"
                    " WHERE task_id = :t AND step_id = :s",
                    {"scope": scope, "t": task_id, "s": step_id, "ts": _now()})
        return self.get_step(user_id, task_id, step_id)

    def reset_external_confirm(self, user_id: str, task_id: str,
                               step_id: str) -> None:
        """步骤参数/内容变化后作废既有确认（重放不能继承上次批准）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE task_steps SET confirmed_at = NULL,"
                " confirmed_scope = NULL, updated_at = :ts"
                " WHERE task_id = :t AND step_id = :s",
                {"t": task_id, "s": step_id, "ts": _now()})

    # -- 重启恢复 -----------------------------------------------------------

    def passivate_running(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """重启恢复：遗留 running 任务 → suspended（明细"重启后中断"），
        其 running/unknown 步骤 → unknown（结果未知，不冒充成功/失败）。"""
        affected: list[dict[str, Any]] = []
        with self._lock, self._conn:
            query = ("SELECT id, user_id FROM tasks WHERE status = 'running'"
                     + (" AND user_id = :u" if user_id else ""))
            params: dict[str, Any] = {"u": user_id} if user_id else {}
            rows = self._conn.execute(query, params).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE tasks SET status = 'suspended',"
                    " detail = '重启后中断，请核对后继续', updated_at = :ts"
                    " WHERE id = :t",
                    {"t": row["id"], "ts": _now()})
                self._conn.execute(
                    "UPDATE task_steps SET status = 'unknown',"
                    " error_code = 'interrupted', updated_at = :ts"
                    " WHERE task_id = :t AND status = 'running'",
                    {"t": row["id"], "ts": _now()})
                task = self.get_task(row["user_id"], row["id"])
                if task is not None:
                    affected.append(task)
        return affected

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _step_out(step: dict[str, Any]) -> dict[str, Any]:
        step["args"] = json.loads(step.pop("args_json") or "{}")
        step["external"] = bool(step.get("external"))
        return step
