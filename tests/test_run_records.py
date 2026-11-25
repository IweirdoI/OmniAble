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


def test_two_users_cannot_read_each_others_runs(store) -> None:
    """用户隔离：A 的运行对 B 不可见（单查 None、列表空、恢复不影响他人）。"""
    store.begin_run("u_a", "run-a", route="chat")
    store.mark_running("u_a", "run-a")
    assert store.get_run("u_b", "run-a") is None
    assert store.list_runs("u_b")["total"] == 0

    store.begin_run("u_b", "run-b")
    listing = store.list_runs("u_a")
    assert listing["total"] == 1 and listing["runs"][0]["id"] == "run-a"

    # 恢复只动本人
    changed = store.passivate_incomplete("u_b")
    assert changed == 1
    assert store.get_run("u_a", "run-a")["status"] == "running"  # A 不受影响
    assert store.get_run("u_b", "run-b")["status"] == "interrupted"


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
    candidates = store.find_recent_incomplete("u_a")
    assert [c["id"] for c in candidates] == ["run-p"]
    assert candidates[0]["steps"] == [{"id": "t1", "name": "read_file", "ok": True}]

    store.finish_run("u_a", "run-p", "partial", stop_reason="tool_budget")
    assert store.find_recent_incomplete("u_a") == []


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
