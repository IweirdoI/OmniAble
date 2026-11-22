"""Agent 本地会话存储与 API 集成回归（TASK-018 / Issue #23）.

Module: agent | 覆盖 Issue 验收口径：
  - 双用户会话/消息/删除互相隔离，切换用户旧请求无法回填
  - 新建、切换、删除；仅恢复成功完成的回合（中断/失败/未核实工具不冒充成功）
  - 遗忘联动：确认删除后相关文本不经恢复或下次请求重新送给模型（幂等）
  - 重启恢复（同库重新打开）；重复操作幂等；旧客户端兼容（不带 session_id）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.server import app, orchestrator, session_store
from src.agent.session_store import FORGOTTEN_PLACEHOLDER, SessionStore

client = TestClient(app)


@pytest.fixture()
def tmp_store(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    yield store
    store.close()


def setup_function() -> None:
    # API 测试换用独立临时库，避免污染全局 session_store
    import tempfile

    fresh = SessionStore(tempfile.mkdtemp(prefix="agent_sessions_") + "/sessions.db")
    session_store._conn.close()
    session_store._conn = fresh._conn
    session_store.path = fresh.path


def _completed_result(answer: str, *, memory_id: str | None = None,
                      stored_ids: list[str] | None = None) -> dict:
    write: dict = {"status": "accepted"}
    if memory_id:
        write["memory_id"] = memory_id
    if stored_ids:
        write["stored_memory_ids"] = stored_ids
    return {"answer": answer, "memory_write": write,
            "workflow": {"status": "completed", "route": "agent"}}


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------


def test_create_list_get_delete_and_restart_recovery(tmp_store) -> None:
    created = tmp_store.create_session("alice", title="下载偏好讨论")
    sid = created["id"]

    assert tmp_store.append_turn(
        "alice", sid,
        user_content="以后文件下载到 D 盘",
        assistant_content="好的，已记录下载路径偏好。",
        memory_id="mem-1",
    ) is True

    # 重启恢复：同一库文件重新打开（模拟进程重启），会话与消息仍在
    reopened = SessionStore(tmp_store.path)
    listing = reopened.list_sessions("alice")
    assert listing["total"] == 1
    session = reopened.get_session("alice", sid)
    assert session is not None
    assert session["title"] == "下载偏好讨论"
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]

    history = reopened.build_model_history("alice", sid)
    assert history == [
        {"role": "user", "content": "以后文件下载到 D 盘"},
        {"role": "assistant", "content": "好的，已记录下载路径偏好。"},
    ]
    reopened.close()


def test_user_isolation_across_all_operations(tmp_store) -> None:
    sid = tmp_store.create_session("alice")["id"]
    tmp_store.append_turn("alice", sid, user_content="alice 的消息",
                          assistant_content="好的")

    # bob 看不到 alice 的会话；操作一律隔离
    assert tmp_store.list_sessions("bob")["total"] == 0
    assert tmp_store.get_session("bob", sid) is None
    assert tmp_store.build_model_history("bob", sid) is None
    assert tmp_store.delete_session("bob", sid) is False
    assert tmp_store.append_turn("bob", sid, user_content="x",
                                 assistant_content="y") is False

    # alice 侧不受影响
    assert tmp_store.list_sessions("alice")["total"] == 1


def test_delete_cascades_messages(tmp_store) -> None:
    sid = tmp_store.create_session("carol")["id"]
    tmp_store.append_turn("carol", sid, user_content="你好",
                          assistant_content="你好，需要什么帮助？")
    assert tmp_store.delete_session("carol", sid) is True
    assert tmp_store.get_session("carol", sid) is None
    assert tmp_store.delete_session("carol", sid) is False  # 重复删除幂等


def test_suppress_by_memory_ids_removes_from_model_history(tmp_store) -> None:
    sid = tmp_store.create_session("dave")["id"]
    tmp_store.append_turn(
        "dave", sid,
        user_content="请记住我的手机号 13812345678",
        assistant_content="已记录你的手机号。",
        memory_id="mem-phone-1",
    )
    tmp_store.append_turn(
        "dave", sid,
        user_content="这条无需记忆",
        assistant_content="好的。",
    )

    history_before = tmp_store.build_model_history("dave", sid)
    assert len(history_before) == 4  # 两回合 × user+assistant

    # 遗忘联动：删除 mem-phone-1 后，引用它的两条消息被抑制（幂等）
    suppressed = tmp_store.suppress_by_memory_ids("dave", ["mem-phone-1"])
    assert suppressed == 2
    assert tmp_store.suppress_by_memory_ids("dave", ["mem-phone-1"]) == 0  # 幂等

    history_after = tmp_store.build_model_history("dave", sid)
    assert history_after == [{"role": "user", "content": "这条无需记忆"},
                             {"role": "assistant", "content": "好的。"}]

    # 展示口径：占位文本，不泄露原文
    display = tmp_store.get_session("dave", sid)
    suppressed_messages = [m for m in display["messages"] if m["suppressed"]]
    assert len(suppressed_messages) == 2
    assert all(m["content"] == FORGOTTEN_PLACEHOLDER for m in suppressed_messages)

    # 跨用户遗忘不影响他人
    sid_b = tmp_store.create_session("erin")["id"]
    tmp_store.append_turn("erin", sid_b, user_content="同文消息",
                          assistant_content="ok", memory_id="mem-phone-1")
    assert tmp_store.suppress_by_memory_ids("dave", ["mem-phone-1"]) == 0
    assert tmp_store.build_model_history("erin", sid_b) != []


def test_suppress_via_stored_memory_ids(tmp_store) -> None:
    """consolidate 晋升产生的 stored_memory_ids 同样参与遗忘联动。"""
    sid = tmp_store.create_session("fred")["id"]
    tmp_store.append_turn(
        "fred", sid,
        user_content="总结一下 ROS2 安装要点",
        assistant_content="要点如下……",
        stored_memory_ids=["mem-consolidated-1"],
    )
    assert tmp_store.suppress_by_memory_ids("fred", ["mem-consolidated-1"]) == 2
    assert tmp_store.build_model_history("fred", sid) == []


# ---------------------------------------------------------------------------
# API 集成（TestClient，MemoryAgent 打桩避免依赖真实模型服务）
# ---------------------------------------------------------------------------


class _FakeAgent:
    """按输入返回固定结果的打桩 Agent；捕获 history 以验证服务端接管。"""

    captured_history: list | None = None

    def __init__(self, *args, **kwargs):
        pass

    def chat(self, request):
        type(self).captured_history = [
            m.model_dump() for m in request.history
        ]
        return _completed_result("已记录。", memory_id=f"mem-{request.message[:6]}")

    def _workflow(self, status, reason=None):
        return {"status": status, "reason": reason}


def test_api_chat_with_session_records_and_builds_history(monkeypatch) -> None:
    from api import server

    monkeypatch.setattr(server, "MemoryAgent", _FakeAgent)
    session_id = client.post(
        "/agent/sessions", json={"user_id": "chat_user", "title": "联调"}
    ).json()["session"]["id"]

    first = client.post("/agent/chat", json={
        "user_id": "chat_user", "message": "以后用中文回答", "session_id": session_id,
    })
    assert first.status_code == 200

    session = client.get(f"/agent/sessions/chat_user/{session_id}").json()["session"]
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]

    second = client.post("/agent/chat", json={
        "user_id": "chat_user", "message": "继续", "session_id": session_id,
    })
    assert second.status_code == 200
    # 第二轮由服务端接管 history（上一回合的两条历史），不依赖客户端携带；
    # 当前消息经 request.message 单独传递，不重复出现在 history 里
    assert _FakeAgent.captured_history == [
        {"role": "user", "content": "以后用中文回答"},
        {"role": "assistant", "content": "已记录。"},
    ]


def test_api_chat_without_session_keeps_legacy_behavior(monkeypatch) -> None:
    """旧客户端不带 session_id：不落库、history 仍由客户端携带（向后兼容）。"""
    from api import server

    monkeypatch.setattr(server, "MemoryAgent", _FakeAgent)
    response = client.post("/agent/chat", json={
        "user_id": "legacy_user", "message": "旧客户端消息",
        "history": [{"role": "user", "content": "更早的消息"}],
    })
    assert response.status_code == 200
    assert session_store.list_sessions("legacy_user")["total"] == 0
    assert _FakeAgent.captured_history == [{"role": "user", "content": "更早的消息"}]


def test_api_forget_suppresses_linked_session_messages(monkeypatch) -> None:
    """遗忘联动：/forget 删除的记忆同步抑制会话内引用（幂等）。"""
    from api import server

    session_id = session_store.create_session("forget_user")["id"]
    session_store.append_turn(
        "forget_user", session_id,
        user_content="请记住我的 API Token",
        assistant_content="已记录。",
        memory_id="mem-token-1",
    )

    monkeypatch.setattr(orchestrator, "forget",
                        lambda uid, instruction: ["mem-token-1"])
    response = client.post("/forget", json={
        "user_id": "forget_user", "instruction": "忘掉我的 Token",
    })
    assert response.status_code == 200

    history = session_store.build_model_history("forget_user", session_id)
    assert history == [], "被遗忘的文本不得经恢复或下次请求重新送给模型"
    display = session_store.get_session("forget_user", session_id)
    assert all(m["suppressed"] for m in display["messages"])


def test_api_chat_unknown_session_returns_404(monkeypatch) -> None:
    from api import server

    monkeypatch.setattr(server, "MemoryAgent", _FakeAgent)
    response = client.post("/agent/chat", json={
        "user_id": "chat_user", "message": "消息", "session_id": "missing-session",
    })
    assert response.status_code == 404


def test_api_session_crud_endpoints() -> None:
    created = client.post("/agent/sessions", json={"user_id": "crud_user"}).json()
    sid = created["session"]["id"]
    assert created["session"]["title"] == "新会话"

    listing = client.get("/agent/sessions/crud_user").json()
    assert listing["total"] == 1

    detail = client.get(f"/agent/sessions/crud_user/{sid}")
    assert detail.status_code == 200
    assert detail.json()["session"]["messages"] == []

    deleted = client.delete(f"/agent/sessions/crud_user/{sid}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert client.get(f"/agent/sessions/crud_user/{sid}").status_code == 404
    assert client.delete(f"/agent/sessions/crud_user/{sid}").status_code == 404


def test_api_non_completed_turns_not_recorded(monkeypatch) -> None:
    """部分完成（未核实工具操作）与失败回合不落库，恢复不冒充成功。"""
    from api import server

    class _PartialAgent(_FakeAgent):
        def chat(self, request):
            return {"answer": "部分完成", "memory_write": {"status": "skipped"},
                    "workflow": {"status": "partial", "route": "agent"}}

    class _ErrorAgent(_FakeAgent):
        def chat(self, request):
            return {"answer": "", "memory_write": {"status": "failed"},
                    "workflow": {"status": "failed", "reason": "model_error"}}

    session_id = session_store.create_session("partial_user")["id"]
    monkeypatch.setattr(server, "MemoryAgent", _PartialAgent)
    client.post("/agent/chat", json={
        "user_id": "partial_user", "message": "带未核实工具的任务", "session_id": session_id,
    })
    monkeypatch.setattr(server, "MemoryAgent", _ErrorAgent)
    client.post("/agent/chat", json={
        "user_id": "partial_user", "message": "失败任务", "session_id": session_id,
    })

    session = session_store.get_session("partial_user", session_id)
    assert session["messages"] == [], "非成功完成回合不得落库"
    assert session_store.build_model_history("partial_user", session_id) == []
