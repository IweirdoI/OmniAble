"""通用任务存储、执行门控与 API 集成回归（TASK-042 / Issue #62）.

Module: agent | 覆盖验收口径：
  - 无需选岗位发起两类样例任务（生活通知日程草稿 / 工作资料字段核验），
    走同一状态机与接管机制
  - 提示协助（assist 不执行）、分步协作、授权代办（external 仍单独确认）
  - 计划、授权范围、实际结果可查；补充信息、暂停、接管
  - 异常停下；unknown 步骤禁止自动重发，核对后才可显式重试
  - 重启恢复：遗留 running → suspended + unknown（不冒充成功）
  - 运行记录关联（route=task，run_record_id 回填步骤）
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

import api.server as server
from api.server import app, run_store, task_store
from src.agent.run_store import RunStore
from src.agent.task_runner import TaskRunner, TaskStepError, build_plan
from src.agent.task_store import TaskStore, TaskStoreError

client = TestClient(app)


@pytest.fixture()
def stores(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    runs = RunStore(tmp_path / "runs.db")
    yield store, runs
    store.close()
    runs.close()


def _make_life_task(store, user="alice", mode="delegate") -> str:
    plan = build_plan("life_schedule", {"text": "周一 09:00 例会\n周三 14:30 复诊"})
    task = store.create_task(user, kind="life_schedule", goal="整理日程草稿",
                             mode=mode, steps=plan,
                             draft={"payload": {"text": "周一 09:00 例会"}})
    return task["id"]


def _make_verify_task(store, user="alice", mode="delegate") -> str:
    plan = build_plan("work_verify", {
        "text": "订单号: A1024\n数量: 3",
        "expected": {"订单号": "A1024", "数量": "5"},
    })
    task = store.create_task(user, kind="work_verify", goal="核验字段",
                             mode=mode, steps=plan, draft={})
    return task["id"]


# ---------------------------------------------------------------------------
# 存储与隔离
# ---------------------------------------------------------------------------

def test_create_and_user_isolation(stores):
    store, _ = stores
    task_id = _make_life_task(store, user="alice")
    task = store.get_task("alice", task_id)
    assert task is not None and task["status"] == "ready"
    assert [s["step_id"] for s in task["steps"]] == ["collect", "organize", "push"]
    assert task["steps"][2]["external"] is True
    assert store.get_task("bob", task_id) is None, "跨用户必须隔离"
    assert store.list_tasks("bob")["total"] == 0
    with pytest.raises(TaskStoreError):
        _make_life_task(store, user="")


def test_invalid_mode_and_empty_plan_rejected(stores):
    store, _ = stores
    with pytest.raises(TaskStoreError):
        store.create_task("alice", kind="x", goal="g", mode="auto",
                          steps=build_plan("life_schedule", {}))
    with pytest.raises(TaskStoreError):
        store.create_task("alice", kind="x", goal="g", mode="step", steps=[])


# ---------------------------------------------------------------------------
# 执行门控
# ---------------------------------------------------------------------------

def test_assist_mode_returns_guidance_without_execution(stores):
    store, _ = stores
    runner = TaskRunner(store)
    task_id = _make_life_task(store, mode="assist")
    task = store.get_task("alice", task_id)
    step_id = task["steps"][0]["step_id"]
    with pytest.raises(TaskStepError) as exc:
        runner.run_step("alice", task_id, step_id)
    assert exc.value.code == "assist_mode"
    assert store.get_step("alice", task_id, step_id)["status"] == "pending"


def test_external_step_requires_confirmation_then_runs(stores):
    store, runs = stores
    runner = TaskRunner(store, run_store=runs)
    task_id = _make_life_task(store)
    push = store.get_task("alice", task_id)["steps"][2]["step_id"]
    with pytest.raises(TaskStepError) as exc:
        runner.run_step("alice", task_id, push)
    assert exc.value.code == "external_confirmation_required"
    assert store.get_task("alice", task_id)["status"] == "waiting_user"
    runner.confirm_external("alice", task_id, push)
    step = runner.run_step("alice", task_id, push)
    assert step["status"] == "done"
    draft = store.get_task("alice", task_id)["draft"]
    assert draft["outbox"]["note"].startswith("本地留痕")


def test_confirmation_invalidated_when_args_change(stores):
    store, _ = stores
    runner = TaskRunner(store)
    task_id = _make_life_task(store)
    push = store.get_task("alice", task_id)["steps"][2]["step_id"]
    runner.confirm_external("alice", task_id, push)
    # 内容变化：作废确认（重放不能继承上次批准）
    store.reset_external_confirm("alice", task_id, push)
    with pytest.raises(TaskStepError) as exc:
        runner.run_step("alice", task_id, push)
    assert exc.value.code == "external_confirmation_required"


def test_unknown_step_blocks_retry_until_review(stores):
    store, _ = stores
    runner = TaskRunner(store)
    task_id = _make_life_task(store)
    organize = store.get_task("alice", task_id)["steps"][1]["step_id"]
    store.mark_step_running("alice", task_id, organize)
    # 模拟中断：步骤置 unknown、任务停 waiting_user（passivate 语义）
    store.finish_step("alice", task_id, organize, status="unknown",
                      error_code="interrupted")
    assert store.get_task("alice", task_id)["status"] == "waiting_user"
    with pytest.raises(TaskStepError) as exc:
        runner.run_step("alice", task_id, organize)
    assert exc.value.code == "review_required"
    runner.review_unknown("alice", task_id, organize)
    assert store.get_step("alice", task_id, organize)["status"] == "pending"
    step = runner.run_step("alice", task_id, organize)
    assert step["status"] == "done"


def test_suspend_blocks_execution_and_resume_waits(stores):
    store, _ = stores
    runner = TaskRunner(store)
    task_id = _make_life_task(store)
    store.set_task_status("alice", task_id, "suspended", detail="暂停")
    with pytest.raises(TaskStoreError):
        store.mark_step_running("alice", task_id, "collect")
    with pytest.raises(TaskStepError) as exc:
        runner.run_step("alice", task_id, "collect")
    assert exc.value.code == "task_not_found" or "不允许执行" in str(exc.value)
    store.set_task_status("alice", task_id, "waiting_user", expect=("suspended",))
    assert store.get_task("alice", task_id)["status"] == "waiting_user"


def test_takeover_switches_to_assist_and_rejects_final(stores):
    store, _ = stores
    task_id = _make_life_task(store)
    task = store.takeover("alice", task_id)
    assert task["mode"] == "assist" and task["status"] == "waiting_user"
    store.set_task_status("alice", task_id, "completed")
    with pytest.raises(TaskStoreError):
        store.takeover("alice", task_id)


# ---------------------------------------------------------------------------
# 两类样例流走同一机制
# ---------------------------------------------------------------------------

def test_life_schedule_flow_reaches_completed(stores):
    store, _ = stores
    runner = TaskRunner(store)
    task_id = _make_life_task(store)
    runner.run_step("alice", task_id, "collect")
    runner.run_step("alice", task_id, "organize")
    draft = store.get_task("alice", task_id)["draft"]
    assert draft["schedule"][0]["time"] == "09:00"
    push = store.get_task("alice", task_id)["steps"][2]["step_id"]
    runner.confirm_external("alice", task_id, push)
    runner.run_step("alice", task_id, push)
    assert store.get_task("alice", task_id)["status"] == "completed"


def test_work_verify_flow_reports_field_diff(stores):
    store, _ = stores
    runner = TaskRunner(store)
    task_id = _make_verify_task(store)
    runner.run_step("alice", task_id, "parse")
    step = runner.run_step("alice", task_id, "verify")
    assert "数量" in (step["result"] or "")
    draft = store.get_task("alice", task_id)["draft"]
    assert draft["verify"]["diffs"][0]["field"] == "数量"


def test_failed_catalog_step_parks_waiting_user(stores):
    store, _ = stores
    runner = TaskRunner(store)
    plan = [{"step_id": "bad", "name": "不可用动作", "action": "web_fetch",
             "args": {"url": "https://example.invalid"}}]
    task = store.create_task("alice", kind="generic", goal="联网步骤",
                             mode="delegate", steps=plan)
    step = runner.run_step("alice", task["id"], "bad")
    assert step["status"] == "failed"
    assert store.get_task("alice", task["id"])["status"] == "waiting_user"


# ---------------------------------------------------------------------------
# 重启恢复与运行记录关联
# ---------------------------------------------------------------------------

def test_passivate_marks_running_step_unknown(stores):
    store, _ = stores
    task_id = _make_life_task(store)
    store.mark_step_running("alice", task_id, "collect")
    affected = store.passivate_running("alice")
    assert [t["id"] for t in affected] == [task_id]
    task = store.get_task("alice", task_id)
    assert task["status"] == "suspended"
    assert task["steps"][0]["status"] == "unknown"
    assert task["steps"][0]["error_code"] == "interrupted"


def test_step_links_run_record(stores):
    store, runs = stores
    runner = TaskRunner(store, run_store=runs)
    task_id = _make_life_task(store)
    step = runner.run_step("alice", task_id, "collect")
    assert step["run_record_id"]
    record = runs.get_run("alice", step["run_record_id"])
    assert record is not None and record["status"] == "completed"
    assert record["route"] == "task"


# ---------------------------------------------------------------------------
# API 集成（TestClient；与 sessions 测试同款临时库注入）
# ---------------------------------------------------------------------------

def setup_function() -> None:
    fresh = TaskStore(tempfile.mkdtemp(prefix="agent_tasks_") + "/tasks.db")
    task_store._conn.close()
    task_store._conn = fresh._conn
    task_store.path = fresh.path
    fresh_runs = RunStore(tempfile.mkdtemp(prefix="agent_task_runs_") + "/runs.db")
    run_store._conn.close()
    run_store._conn = fresh_runs._conn
    run_store.path = fresh_runs.path
    server.task_runner.store = task_store
    server.task_runner.run_store = run_store


def test_api_create_detail_and_happy_path():
    resp = client.post("/agent/tasks", json={
        "user_id": "alice", "kind": "life_schedule", "goal": "整理日程",
        "mode": "delegate", "payload": {"text": "周一 09:00 例会"}})
    assert resp.status_code == 200
    task = resp.json()["task"]
    task_id = task["id"]
    assert [s["step_id"] for s in task["steps"]][0] == "collect"

    detail = client.get(f"/agent/tasks/alice/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["task"]["status"] == "ready"
    assert client.get(f"/agent/tasks/bob/{task_id}").status_code == 404

    push = [s for s in detail.json()["task"]["steps"] if s["external"]][0]["step_id"]
    # 外发未确认 → 409 且任务停等待用户
    blocked = client.post(f"/agent/tasks/alice/{task_id}/steps/{push}/run")
    assert blocked.status_code == 409
    assert client.get(f"/agent/tasks/alice/{task_id}").json()["task"]["status"] == "waiting_user"

    confirmed = client.post(f"/agent/tasks/alice/{task_id}/steps/{push}/confirm")
    assert confirmed.status_code == 200
    done = client.post(f"/agent/tasks/alice/{task_id}/steps/{push}/run")
    assert done.status_code == 200
    assert done.json()["step"]["status"] == "done"
    # 其余步骤仍 pending → ready；全部跑完 → completed
    assert client.get(f"/agent/tasks/alice/{task_id}").json()["task"]["status"] == "ready"
    for sid in ("collect", "organize"):
        step = client.post(f"/agent/tasks/alice/{task_id}/steps/{sid}/run")
        assert step.status_code == 200
        assert step.json()["step"]["status"] == "done"
    assert client.get(f"/agent/tasks/alice/{task_id}").json()["task"]["status"] == "completed"


def test_api_suspend_resume_takeover_and_draft():
    task_id = client.post("/agent/tasks", json={
        "user_id": "alice", "kind": "work_verify", "goal": "核验字段",
        "mode": "delegate",
        "payload": {"text": "订单号: A1024", "expected": {"订单号": "A1024"}},
    }).json()["task"]["id"]

    assert client.post(f"/agent/tasks/alice/{task_id}/suspend").status_code == 200
    run_while_suspended = client.post(
        f"/agent/tasks/alice/{task_id}/steps/parse/run")
    assert run_while_suspended.status_code == 409
    assert client.post(f"/agent/tasks/alice/{task_id}/resume").status_code == 200
    assert client.post(f"/agent/tasks/alice/{task_id}/takeover").status_code == 200
    task = client.get(f"/agent/tasks/alice/{task_id}").json()["task"]
    assert task["mode"] == "assist" and task["status"] == "waiting_user"

    draft = client.post(f"/agent/tasks/alice/{task_id}/draft",
                        json={"user_id": "alice", "draft": {"note": "补充说明"}})
    assert draft.status_code == 200
    assert draft.json()["task"]["draft"]["note"] == "补充说明"


def test_api_unknown_review_and_retry():
    task_id = client.post("/agent/tasks", json={
        "user_id": "alice", "kind": "life_schedule", "goal": "整理日程",
        "mode": "delegate", "payload": {"text": "周一 09:00 例会"},
    }).json()["task"]["id"]
    # 模拟中断 → unknown
    task_store.mark_step_running("alice", task_id, "organize")
    task_store.finish_step("alice", task_id, "organize", status="unknown",
                           error_code="interrupted")
    run_unknown = client.post(f"/agent/tasks/alice/{task_id}/steps/organize/run")
    assert run_unknown.status_code == 409
    assert run_unknown.json()["detail"]["code"] == "review_required"
    assert client.post(
        f"/agent/tasks/alice/{task_id}/steps/organize/review").status_code == 200
    retried = client.post(f"/agent/tasks/alice/{task_id}/steps/organize/run")
    assert retried.status_code == 200
    assert retried.json()["step"]["status"] == "done"


def test_api_recover_after_restart():
    task_id = client.post("/agent/tasks", json={
        "user_id": "alice", "kind": "life_schedule", "goal": "整理日程",
        "mode": "delegate", "payload": {"text": "周一 09:00 例会"},
    }).json()["task"]["id"]
    task_store.mark_step_running("alice", task_id, "collect")
    recovered = client.post("/agent/tasks/alice/recover")
    assert recovered.status_code == 200
    task = recovered.json()["recovered"][0]
    assert task["status"] == "suspended"
    assert task["steps"][0]["status"] == "unknown"
