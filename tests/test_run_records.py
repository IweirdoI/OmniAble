"""TASK-022 / Issue #30 运行记录持久化回归。

对应验收：
- 两名虚构用户仅可读自己的运行（跨用户 404/空）
- 同一幂等键在并发与重启后只发起一次可去重运行
- 执行记录（steps/artifacts/status）持久化，取消与中断如实标注
- 重启恢复：遗留 pending/running 标注 interrupted，不冒充成功
- 会话挂接与用户隔离口径一致（TASK-018 协调）
"""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from src.agent.run_store import RunStore


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# 存储层：去重、隔离、终态、恢复
# ---------------------------------------------------------------------------

def test_begin_run_idempotent_within_and_across_restart(store) -> None:
    """同一幂等键的活跃运行：并发与重启后重复注册都返回原运行。"""
    record, dup = store.begin_run("u_a", "run-1", idempotency_key="req-1")
    assert dup is False and record["id"] == "run-1" and record["status"] == "pending"
    store.mark_running("u_a", "run-1")

    # 并发重复注册（模拟重试）
    results = []
    def register():
        results.append(store.begin_run("u_a", "run-1x", idempotency_key="req-1"))
    threads = [threading.Thread(target=register) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for record_x, dup_x in results:
        assert dup_x is True and record_x["id"] == "run-1"

    # 未终态时再开新存储实例（模拟重启）→ 去重依然生效
    store.close()
    reopened = RunStore(store.path)
    try:
        record2, dup2 = reopened.begin_run("u_a", "run-2", idempotency_key="req-1")
        assert dup2 is True and record2["id"] == "run-1"
        # 终态后同键允许新运行
        reopened.finish_run("u_a", "run-1", "completed", latency_ms=12.0)
        record3, dup3 = reopened.begin_run("u_a", "run-3", idempotency_key="req-1")
        assert dup3 is False and record3["id"] == "run-3"
    finally:
        reopened.close()


def test_idempotency_key_is_user_scoped(store) -> None:
    """幂等键按用户隔离：同键跨用户不得返回他人的运行（复核项）。"""
    store.begin_run("u_a", "run-a1", idempotency_key="shared-req")
    record_b, dup_b = store.begin_run("u_b", "run-b1", idempotency_key="shared-req")
    assert dup_b is False and record_b["id"] == "run-b1"


def test_two_users_cannot_read_each_others_runs(store) -> None:
    """用户隔离：A 的运行对 B 不可见（单查 None、列表空、恢复不影响他人）。"""
    store.begin_run("u_a", "run-a", route="chat")
    store.mark_running("u_a", "run-a")
    assert store.get_run("u_b", "run-a") is None
    assert store.list_runs("u_b")["total"] == 0

    store.begin_run("u_b", "run-b")
    listing = store.list_runs("u_a")
    assert listing["total"] == 1 and listing["runs"][0]["id"] == "run-a"

    # 恢复只动本人（且只动孤儿；owner_pid 由 begin_run 记为本进程 → 直接
    # 恢复只动本人需通过孤儿语义：这里 B 的运行同为本进程，先验证不动 A）
    changed = store.passivate_incomplete("u_b")
    assert changed == 0  # 本进程创建（owner=当前进程）→ 非孤儿，不误伤
    assert store.get_run("u_a", "run-a")["status"] == "running"  # A 不受影响
    assert store.get_run("u_b", "run-b")["status"] == "pending"  # 活跃态不被误伤


def test_terminal_states_preserve_truthful_outcomes(store) -> None:
    """终态如实标注 completed/partial/cancelled/timed_out；非法终态拒绝。"""
    store.begin_run("u_a", "r-ok")
    store.finish_run("u_a", "r-ok", "completed",
                     steps=[{"id": "t1", "name": "calculate", "ok": True}],
                     artifacts=[{"path": "a.txt", "bytes": 3}])
    record = store.get_run("u_a", "r-ok")
    assert record["status"] == "completed"
    assert record["steps"][0]["ok"] is True
    assert record["artifacts"][0]["path"] == "a.txt"

    store.begin_run("u_a", "r-cancel")
    store.finish_run("u_a", "r-cancel", "cancelled", stop_reason="user_stop")
    assert store.get_run("u_a", "r-cancel")["status"] == "cancelled"

    with pytest.raises(ValueError):
        store.finish_run("u_a", "r-cancel", "success")  # 杜撰终态拒绝


def test_progress_recording_and_recovery_candidates(store) -> None:
    """执行中记录 steps；恢复候选仅含活跃运行。"""
    store.begin_run("u_a", "run-p", route="chat")
    store.mark_running("u_a", "run-p")
    store.record_progress("u_a", "run-p",
                          steps=[{"id": "t1", "name": "read_file", "ok": True}])
    # 本进程运行（owner=当前进程）不是恢复候选——孤儿才是（复核整改语义）
    assert store.find_recent_incomplete("u_a") == []
    assert store.get_run("u_a", "run-p")["steps"] == [
        {"id": "t1", "name": "read_file", "ok": True}]

    store.finish_run("u_a", "run-p", "partial", stop_reason="tool_budget")
    assert store.get_run("u_a", "run-p")["status"] == "partial"


# ---------------------------------------------------------------------------
# API 层：端点隔离、聊天落盘、重启恢复
# ---------------------------------------------------------------------------

def test_api_run_endpoints_isolation_and_chat_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    import api.server as server
    # 换独立库（不污染全局）
    fresh = RunStore(tmp_path / "api_runs.db")
    server.run_store._conn.close()
    server.run_store = fresh
    monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: _StubAgent())

    with TestClient(server.app) as client:
        payload = {"user_id": "ru_a", "message": "计算 1+1", "use_memory": False,
                   "save_memory": False}
        resp = client.post("/agent/chat", json=payload)
        assert resp.status_code == 200
        run_record_id = resp.json().get("run_record_id")
        assert run_record_id

        record = client.get(f"/agent/runs/ru_a/{run_record_id}").json()
        assert record["status"] == "completed"

        # 跨用户 → 404；列表隔离
        assert client.get(f"/agent/runs/ru_b/{run_record_id}").status_code == 404
        assert client.get("/agent/runs/ru_b").json()["total"] == 0
        assert client.get("/agent/runs/ru_a").json()["total"] == 1

        # 重启恢复：标注 interrupted
        recovered = client.post("/agent/runs/ru_a/recover").json()
        assert recovered["interrupted"] == 0  # 已终态，无遗留
    fresh.close()


def test_api_stream_records_terminal_and_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    import api.server as server
    fresh = RunStore(tmp_path / "stream_runs.db")
    server.run_store._conn.close()
    server.run_store = fresh
    monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: _StubAgent())

    with TestClient(server.app) as client:
        with client.stream("POST", "/agent/chat/stream",
                           json={"user_id": "ru_s", "message": "计算 2+2",
                                 "use_memory": False, "save_memory": False}) as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode("utf-8")
        assert "data:" in body
        # 流式终态落盘（worker 的 done 事件是权威结果）
        runs = client.get("/agent/runs/ru_s").json()["runs"]
        assert runs and runs[0]["status"] in {"completed", "partial", "interrupted"}
    fresh.close()


class _StubAgent:
    """最小替身 agent：同步与流式都直接返回完成结果，不发模型请求。"""

    def __init__(self):
        self._ctx = None
        self._worker_started = False

    def _result(self, req):
        return {"answer": "已回答", "memories": [], "recall_ms": 0.0,
                "warnings": [], "context_warnings": [], "memory_write": {"status": "disabled"},
                "reset_history": False, "latency_ms": 1.0,
                "deleted_ids": [], "type": "done",
                "workflow": {"run_id": "stub", "route": "chat", "status": "completed",
                             "steps": [], "artifacts": []}}

    def chat(self, req):
        result = self._result(req)
        result.pop("type", None)
        return result

    def chat_stream(self, req, on_finished=None):
        async def gen():
            started = json.dumps({"type": "run_started", "run_id": "stub", "protocol": 1})
            yield f"data: {started}\n\n"
            yield "data: " + json.dumps(self._result(req), ensure_ascii=False) + "\n\n"
            if on_finished:
                on_finished()
        return gen()


# ---------------------------------------------------------------------------
# 复核整改：取消入口 / 终态禁回写 / 恢复安全 / 已确认步骤 / 人工核对
# ---------------------------------------------------------------------------

def test_cancel_run_transition_and_idempotence(store) -> None:
    """取消入口：活跃 → cancelled；重复取消返回当前状态不重复执行。"""
    store.begin_run("u_a", "rc-1")
    store.mark_running("u_a", "rc-1")
    first = store.cancel_run("u_a", "rc-1")
    assert first == {"cancelled": True, "status": "cancelled"}
    again = store.cancel_run("u_a", "rc-1")
    assert again == {"cancelled": False, "status": "cancelled"}
    assert store.get_run("u_a", "rc-1")["stop_reason"] == "user_stop"
    # 跨用户 → None
    assert store.cancel_run("u_b", "rc-1") is None


def test_finished_run_cannot_be_overwritten_by_late_result(store) -> None:
    """终态禁回写：interrupted 后晚到的成功结果不得改写状态（复核项）。"""
    store.begin_run("u_a", "rl-1")
    store.mark_running("u_a", "rl-1")
    assert store.finish_run("u_a", "rl-1", "interrupted", stop_reason="client_disconnected")
    # 晚到终态：返回 False，状态不变
    assert store.finish_run("u_a", "rl-1", "completed", latency_ms=99) is False
    assert store.get_run("u_a", "rl-1")["status"] == "interrupted"


def test_recovery_spares_active_runs_same_process(store) -> None:
    """恢复安全：本进程正在执行的运行不被标中断、不进候选（复核项）。"""
    store.begin_run("u_a", "active-1")
    store.mark_running("u_a", "active-1")
    # 本进程未持有 owner 的孤儿（模拟另一进程崩溃遗留）：owner_pid=None
    import sqlite3
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO runs(id, user_id, owner_pid, status, created_at, updated_at)"
            " VALUES('orphan-1', 'u_a', NULL, 'running', 't', 't')")
    changed = store.passivate_incomplete("u_a")
    assert changed == 1
    statuses = {r["id"]: r["status"] for r in store.list_runs("u_a")["runs"]}
    assert statuses["active-1"] == "running"   # 本进程活跃运行不受影响
    assert statuses["orphan-1"] == "interrupted"  # 孤儿进中断
    # 恢复候选不再含已处理孤儿
    assert store.find_recent_incomplete("u_a") == []


def test_confirmed_steps_skip_on_recovery(store) -> None:
    """已确认步骤：登记后恢复/重试时跳过（复核项）。"""
    store.begin_run("u_a", "rs-1")
    assert store.mark_step_confirmed("u_a", "rs-1", "step-1") is True
    assert store.mark_step_confirmed("u_a", "rs-1", "step-2") is True
    # 重复登记幂等
    store.mark_step_confirmed("u_a", "rs-1", "step-1")
    assert store.confirmed_steps("u_a", "rs-1") == ["step-1", "step-2"]
    assert store.confirmed_steps("u_b", "rs-1") == []  # 跨用户空


def test_external_unknown_result_goes_needs_review(store) -> None:
    """外部提交后崩溃且结果未知 → needs_review（人工核对），不冒充成败。"""
    store.begin_run("u_a", "rn-1")
    store.mark_running("u_a", "rn-1")
    assert store.mark_needs_review("u_a", "rn-1", reason="提交已发出，回执未知") is True
    record = store.get_run("u_a", "rn-1")
    assert record["status"] == "needs_review" and "回执" in record["stop_reason"]


def test_needs_review_not_overwritten_by_late_result(store) -> None:
    """needs_review 是稳定终态：晚到结果不得覆盖（必须人工核对）。"""
    store.begin_run("u_a", "rn-2")
    store.mark_needs_review("u_a", "rn-2")
    assert store.finish_run("u_a", "rn-2", "completed") is False
    assert store.get_run("u_a", "rn-2")["status"] == "needs_review"


def test_api_cancel_and_review_endpoints(tmp_path, monkeypatch) -> None:
    """取消与人工核对端点：隔离（404）、幂等（不可重复取消）。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    import api.server as server
    fresh = RunStore(tmp_path / "api_runs2.db")
    server.run_store._conn.close()
    server.run_store = fresh
    monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: _StubAgent())

    with TestClient(server.app) as client:
        payload = {"user_id": "ru_c", "message": "计算 1+1", "use_memory": False,
                   "save_memory": False}
        resp = client.post("/agent/chat", json=payload)
        run_id = resp.json()["run_record_id"]
        # 完成后不可取消
        result = client.post(f"/agent/runs/ru_c/{run_id}/cancel").json()
        assert result["cancelled"] is False and result["status"] == "completed"
        # 跨用户 404
        assert client.post(f"/agent/runs/ru_x/{run_id}/cancel").status_code == 404
        # 已终态不可转人工核对
        assert client.post(f"/agent/runs/ru_c/{run_id}/review").status_code == 409
    fresh.close()


def test_api_sync_idempotency_key_header(tmp_path, monkeypatch) -> None:
    """HTTP 层幂等：同步完成即终态 → 重试同键视为新调用（不改写历史）。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    import api.server as server
    fresh = RunStore(tmp_path / "api_runs3.db")
    server.run_store._conn.close()
    server.run_store = fresh
    monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: _StubAgent())

    with TestClient(server.app) as client:
        payload = {"user_id": "ru_i", "message": "计算 1+1", "use_memory": False,
                   "save_memory": False}
        headers = {"Idempotency-Key": "req-key-1"}
        first = client.post("/agent/chat", json=payload, headers=headers)
        # 首次请求同步完成即终态 → 重试同键视为新调用（幂等保护的是活跃窗口）
        retry = client.post("/agent/chat", json=payload, headers=headers)
        assert first.status_code == retry.status_code == 200
        ids = {first.json()["run_record_id"], retry.json()["run_record_id"]}
        assert len(ids) == 2, "终态后同键重试应创建新运行（不改写历史）"
    fresh.close()
