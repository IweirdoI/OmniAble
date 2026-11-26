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
        # 终态后同键重试：标准幂等——仍返回首次记录（不开启新运行）
        reopened.finish_run("u_a", "run-1", "completed", latency_ms=12.0)
        record3, dup3 = reopened.begin_run("u_a", "run-3", idempotency_key="req-1")
        assert dup3 is True and record3["id"] == "run-1"
        assert record3["status"] == "completed"
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
    assert changed == []  # 本进程创建（owner=当前进程）→ 非孤儿，不误伤
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

    def chat(self, req, on_ctx_ready=None):
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
    # 返回本次标注的候选（复核项：恢复须返回刚标中断的记录）
    assert [c["id"] for c in changed] == ["orphan-1"]
    assert changed[0]["status"] == "interrupted"
    assert changed[0]["confirmed_steps"] == []
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
        # 标准幂等：同键重试（无论首次是否终态）返回首次运行标识
        retry = client.post("/agent/chat", json=payload, headers=headers)
        assert first.status_code == 200
        assert retry.status_code == 409  # 活跃窗口 409；终态后同样复用首次
        assert retry.json()["run_record_id"] == first.json()["run_record_id"]
        assert retry.json()["duplicate"] is True
    fresh.close()


# ---------------------------------------------------------------------------
# 复核项：取消必须传递到活跃 RunContext（步骤启动边界拦截）
# ---------------------------------------------------------------------------

def test_cancel_propagates_to_active_run_context() -> None:
    """取消入口 → ActiveRunRegistry 协作取消 → 下一步骤启动边界抛 RunAborted。"""
    from src.agent.run_store import active_runs
    from src.agent.runtime import RunAborted, RunContext

    ctx = RunContext(route="tool_task")
    registry = active_runs()
    registry.register("rc-live", ctx)
    try:
        # 取消前：步骤边界可正常通过
        ctx.check("before_tool")
        # 触发协作取消（与 POST /agent/runs/{u}/{r}/cancel 的路径一致）
        assert registry.cancel("rc-live", reason="user_stop") is True
        assert ctx.cancelled is True
        # 下一步骤启动边界：拦截（执行器不会启动下一步）
        with pytest.raises(RunAborted):
            ctx.check("before_tool")
    finally:
        registry.unregister("rc-live")
    # 注销后取消不再命中
    assert registry.cancel("rc-live") is False


def test_cancel_endpoint_cancels_active_run(tmp_path, monkeypatch) -> None:
    """API 取消端点：命中活跃 RunContext 时传递取消（不是只改数据库）。"""
    import api.server as server
    import threading as _threading
    from src.agent.run_store import active_runs
    from src.agent.runtime import RunContext

    fresh = RunStore(tmp_path / "cancel_run.db")
    server.run_store._conn.close()
    server.run_store = fresh
    ctx = RunContext(route="tool_task")
    record, _ = fresh.begin_run("ru_live", "live-1", route="tool_task")
    fresh.mark_running("ru_live", "live-1")
    active_runs().register("live-1", ctx)
    try:
        with TestClient(server.app) as client:
            resp = client.post("/agent/runs/ru_live/live-1/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is True and body["status"] == "cancelled"
        assert ctx.cancelled is True  # 取消已触达执行器
    finally:
        active_runs().unregister("live-1")
        fresh.close()


# ---------------------------------------------------------------------------
# 复核项：同一持久操作键并发/重启后稳定去重（不开启重复运行）
# ---------------------------------------------------------------------------

def test_same_key_concurrent_begin_never_opens_two_runs(store) -> None:
    """同用户同键并发注册：唯一（用户,键）索引下绝不出现两条活跃运行。"""
    import threading as _threading
    results = []

    def worker(i):
        record, dup = store.begin_run("u_a", f"race-{i}", idempotency_key="same-key")
        results.append((record["id"], dup))

    threads = [_threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {record_id for record_id, _ in results}
    dups = [dup for _, dup in results]
    assert len(ids) == 1, f"同键并发不得开启多个运行：{ids}"
    assert sum(1 for d in dups if d) == 7, "其余 7 个必须去重返回既有运行"
    active = [r for r in store.list_runs("u_a")["runs"] if r["status"] in ("pending", "running")]
    assert len(active) == 1


def test_recovery_returns_confirmed_steps_for_skip(store) -> None:
    """恢复候选携带已确认步骤（重试按此跳过，不重复执行）。"""
    import sqlite3 as _sq
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO runs(id, user_id, owner_pid, status, steps_json,"
            " confirmed_steps_json, created_at, updated_at)"
            " VALUES('orphan-2', 'u_a', NULL, 'running', '[]', '[\"row-1\", \"row-2\"]', 't', 't')")
    candidates = store.passivate_incomplete("u_a")
    assert len(candidates) == 1
    assert candidates[0]["confirmed_steps"] == ["row-1", "row-2"]


def test_same_key_after_terminal_concurrent_never_opens_two(store) -> None:
    """复测场景：首轮结束后，同键并发重试绝不开启两条活跃运行。"""
    import threading as _threading
    # 首轮：创建并终态
    record, _ = store.begin_run("u_a", "first-1", idempotency_key="op-key")
    store.mark_running("u_a", "first-1")
    store.finish_run("u_a", "first-1", "completed")

    results = []

    def worker(i):
        r, dup = store.begin_run("u_a", f"retry-{i}", idempotency_key="op-key")
        results.append((r["id"], dup))

    threads = [_threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {rid for rid, _ in results}
    assert ids == {"first-1"}, f"终态后同键并发必须全部复用首次：{ids}"
    assert all(dup for _, dup in results)
    active = [r for r in store.list_runs("u_a")["runs"]
              if r["status"] in ("pending", "running")]
    assert active == [], "不得出现新的活跃运行"


def test_sync_entry_registers_ctx_and_cancel_reaches_executor(tmp_path, monkeypatch) -> None:
    """真实同步入口：ctx 经 on_ctx_ready 注册，取消端点触达执行器。"""
    import api.server as server
    from src.agent.run_store import active_runs
    from src.agent.runtime import RunAborted

    fresh = RunStore(tmp_path / "sync_cancel.db")
    server.run_store._conn.close()
    server.run_store = fresh

    captured = {}

    class _CancelAwareAgent:
        """替身：chat() 内创建 ctx 并回调注册；执行中检查取消（模拟步骤边界）。"""

        def __init__(self):
            self._ctx = None

        def _workflow(self, status, reason=None):
            return {"run_id": "s", "route": "chat", "status": status,
                    "stop_reason": reason, "steps": [], "artifacts": []}

        def chat(self, req, on_ctx_ready=None):
            from src.agent.runtime import RunContext
            self._ctx = RunContext(route="chat")
            if on_ctx_ready is not None:
                on_ctx_ready(self._ctx)
            captured["ctx"] = self._ctx
            # 模拟执行器：下一步骤启动边界检查
            self._ctx.check("before_tool")
            return {"answer": "ok", "memories": [], "recall_ms": 0.0, "warnings": [],
                    "context_warnings": [], "memory_write": {"status": "disabled"},
                    "reset_history": False, "latency_ms": 1.0,
                    "workflow": {"run_id": "s", "route": "chat", "status": "completed",
                                 "steps": [], "artifacts": []}}

    monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: _CancelAwareAgent())
    with TestClient(server.app) as client:
        # 先发起同步请求（后台线程），等 ctx 注册后取消
        import threading as _threading
        result_holder = {}

        def run_chat():
            try:
                result_holder["resp"] = client.post("/agent/chat", json={
                    "user_id": "ru_sync", "message": "计算 1+1",
                    "use_memory": False, "save_memory": False})
            except Exception as exc:  # noqa: BLE001
                result_holder["error"] = exc

        # 用一个可控替身：注册后阻塞在 check 前，由测试线程取消
        class _BlockingAgent(_CancelAwareAgent):
            def chat(self, req, on_ctx_ready=None):
                from src.agent.runtime import RunContext
                self._ctx = RunContext(route="chat")
                if on_ctx_ready is not None:
                    on_ctx_ready(self._ctx)
                captured["ctx"] = self._ctx
                captured["ready"].set()
                captured["release"].wait(5)
                self._ctx.check("before_tool")  # 取消后此处抛 RunAborted
                return _CancelAwareAgent.chat(self, req, None)

        captured["ready"] = _threading.Event()
        captured["release"] = _threading.Event()
        monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: _BlockingAgent())

        thread = _threading.Thread(target=run_chat)
        thread.start()
        assert captured["ready"].wait(5), "ctx 未注册"
        # 找到活跃 run_id 并取消（模拟 POST /agent/runs/{u}/{r}/cancel）
        with active_runs()._lock:
            run_id = next(iter(active_runs()._contexts))
        resp = client.post(f"/agent/runs/ru_sync/{run_id}/cancel")
        assert resp.json()["cancelled"] is True
        captured["release"].set()
        thread.join(timeout=10)
        # 同步入口捕获 RunAborted → 409 结构化取消结果（不 500）
        assert "resp" in result_holder, f"请求线程异常：{result_holder.get('error')!r}"
        assert result_holder["resp"].status_code == 409
        assert result_holder["resp"].json()["cancelled"] is True
        assert fresh.get_run("ru_sync", run_id)["status"] == "cancelled"
    fresh.close()
