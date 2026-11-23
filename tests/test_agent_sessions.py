"""Agent 本地会话存储与 API 集成回归（TASK-018 / Issue #23）.

Module: agent | 覆盖 Issue 验收口径：
  - 双用户会话/消息/删除互相隔离，切换用户旧请求无法回填
  - 新建、切换、删除；仅恢复成功完成的回合（中断/失败/未核实工具不冒充成功）
  - 遗忘联动：确认删除后相关文本不经恢复或下次请求重新送给模型（幂等）
  - 重启恢复（同库重新打开）；重复操作幂等；旧客户端兼容（不带 session_id）
"""

from __future__ import annotations

import json
import sqlite3

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


def _install_streaming_agent(monkeypatch, *, answer: str = "已记录下载路径。"):
    """用真实 MemoryAgent + 打桩模型安装到 API：覆盖真实 SSE 异步迭代路径。

    不能用 MemoryAgent 打桩替代——复核阻塞项 1 正是同步 for 迭代异步生成器，
    只有走真实的 chat_stream 异步生成器才能复现与验证。
    """
    from api import server
    from src.agent.service import ChatRequest as _CR
    from src.agent.service import MemoryAgent as _MA

    class StreamModel:
        tools_enabled = False
        timeout = 60.0

        def complete_stream(self, messages, tools, timeout=None):
            yield {"kind": "token", "text": answer}
            yield {"kind": "message",
                   "message": {"role": "assistant", "content": answer}}

    def factory(orchestrator, **kwargs):
        return _MA(orchestrator, StreamModel(), stream=kwargs.get("stream", False))

    monkeypatch.setattr(server, "MemoryAgent", factory)
    return _CR


def test_stream_endpoint_with_session_returns_body_and_records(monkeypatch) -> None:
    """真实 SSE 入口：带 session_id 必须返回非空正文并落库（阻塞项 1）。"""
    _install_streaming_agent(monkeypatch)
    sid = session_store.create_session("stream_user")["id"]

    response = client.post("/agent/chat/stream", json={
        "user_id": "stream_user", "message": "以后文件下载到 D 盘", "session_id": sid,
    })

    assert response.status_code == 200
    body = response.text
    assert body.strip(), "流式响应不得为空正文（同步 for 迭代异步生成器会导致此问题）"
    assert "data: " in body

    events = [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert events, "应至少产出一条 SSE 事件"
    done = next((e for e in events if e.get("type") == "done"), None)
    assert done is not None, f"缺少 done 终态：{[e.get('type') for e in events]}"
    assert done.get("answer"), "done 事件应包含非空回答"

    recorded = session_store.get_session("stream_user", sid)
    assert [m["role"] for m in recorded["messages"]] == ["user", "assistant"], (
        "带 session_id 的流式成功回合必须落库"
    )
    assert recorded["messages"][1]["content"] == done["answer"]


def test_stream_wrapper_skips_record_on_client_disconnect(monkeypatch) -> None:
    """断线回归：流被提前关闭（无 done 终态）时不落库，恢复不冒充成功。"""
    _install_streaming_agent(monkeypatch)
    from src.agent.service import ChatRequest as ChatReq, MemoryAgent as Agent
    from api.server import _record_session_stream, orchestrator

    sid = session_store.create_session("disconnect_user")["id"]

    class StreamModel:
        tools_enabled = False
        timeout = 60.0

        def complete_stream(self, messages, tools, timeout=None):
            yield {"kind": "token", "text": "开"}
            yield {"kind": "message", "message": {"role": "assistant", "content": "开始回答"}}

    async def scenario():
        agent = Agent(orchestrator, StreamModel(), stream=True)
        req = ChatReq(user_id="disconnect_user", message="中途断线", session_id=sid)
        gen = _record_session_stream(agent.chat_stream(req), req)
        first = await gen.__anext__()          # 只取第一块，模拟客户端提前关闭
        await gen.aclose()                     # 触发 finally：无 done 终态
        return first

    import asyncio
    first_chunk = asyncio.run(scenario())
    assert first_chunk, "断线前至少应拿到首个事件块"
    assert session_store.get_session("disconnect_user", sid)["messages"] == [], (
        "断线（无 done 终态）不得落库"
    )


def test_api_recalled_memory_ids_drive_suppression(monkeypatch) -> None:
    """阻塞项 2：删除「回答引用的召回记忆」后，旧回答必须退出模型历史。"""
    from api import server

    class _RecallAgent(_FakeAgent):
        def chat(self, request):
            return {
                "answer": "按你的 D 盘偏好，建议先改配置。",
                "memory_write": {"status": "skipped"},
                "referenced_memory_ids": ["mem-recall-1", "mem-recall-2"],
                "workflow": {"status": "completed", "route": "agent"},
            }

    monkeypatch.setattr(server, "MemoryAgent", _RecallAgent)
    sid = session_store.create_session("recall_user")["id"]

    response = client.post("/agent/chat", json={
        "user_id": "recall_user", "message": "下载路径怎么设", "session_id": sid,
    })
    assert response.status_code == 200
    assert session_store.build_model_history("recall_user", sid) == [
        {"role": "user", "content": "下载路径怎么设"},
        {"role": "assistant", "content": "按你的 D 盘偏好，建议先改配置。"},
    ]

    suppressed = session_store.suppress_by_memory_ids("recall_user", ["mem-recall-1"])
    assert suppressed == 2, "召回的任意一条记忆被删，整轮（user+assistant）都应抑制"
    assert session_store.build_model_history("recall_user", sid) == [], (
        "旧回答不得留在模型历史中"
    )
    assert session_store.suppress_by_memory_ids("recall_user", ["mem-recall-2"]) == 0  # 幂等


def test_api_forget_turn_is_not_recorded(monkeypatch) -> None:
    """阻塞项 2：遗忘指令回合本身不得落库（reset_history / route=forget）。"""
    from api import server

    class _ForgetAgent(_FakeAgent):
        def chat(self, request):
            return {
                "answer": "已处理遗忘指令，删除 1 条记忆。",
                "memory_write": {"status": "skipped"},
                "reset_history": True,
                "deleted_ids": ["mem-old-1"],
                "workflow": {"status": "completed", "route": "forget"},
            }

    monkeypatch.setattr(server, "MemoryAgent", _ForgetAgent)
    sid = session_store.create_session("forget_turn_user")["id"]

    response = client.post("/agent/chat", json={
        "user_id": "forget_turn_user", "message": "忘掉我的下载路径偏好", "session_id": sid,
    })
    assert response.status_code == 200
    assert session_store.get_session("forget_turn_user", sid)["messages"] == [], (
        "遗忘指令不得作为对话记录保存"
    )
    assert session_store.build_model_history("forget_turn_user", sid) == []


def test_restart_recovery_after_suppression(tmp_store) -> None:
    """重启恢复：抑制状态与召回关联均需持久化到磁盘。"""
    sid = tmp_store.create_session("restart_user")["id"]
    tmp_store.append_turn(
        "restart_user", sid,
        user_content="问题 A",
        assistant_content="回答 A",
        referenced_memory_ids=["mem-r-1"],
    )
    tmp_store.append_turn(
        "restart_user", sid,
        user_content="问题 B",
        assistant_content="回答 B",
    )
    tmp_store.suppress_by_memory_ids("restart_user", ["mem-r-1"])

    reopened = SessionStore(tmp_store.path)
    history = reopened.build_model_history("restart_user", sid)
    assert history == [
        {"role": "user", "content": "问题 B"},
        {"role": "assistant", "content": "回答 B"},
    ], "重启后被抑制的回合仍须排除在模型历史之外"
    reopened.close()


def test_schema_v1_database_is_migrated(tmp_path) -> None:
    """旧库兼容：v1 表结构（无 referenced_memory_ids）自动补列，旧记录可读。"""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
            memory_id TEXT, stored_memory_ids TEXT,
            suppressed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO sessions VALUES ('s1','legacy','旧会话','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
        INSERT INTO messages VALUES ('m1','s1','user','旧消息','completed',NULL,NULL,0,'2026-01-01T00:00:00Z');
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()

    store = SessionStore(path)
    session = store.get_session("legacy", "s1")
    assert session is not None and session["messages"][0]["content"] == "旧消息"
    assert store.build_model_history("legacy", "s1") == [
        {"role": "user", "content": "旧消息"}
    ]
    store.close()


def test_forget_without_session_id_suppresses_saved_sessions(monkeypatch) -> None:
    """复核阻塞项 1：不带 session_id 的遗忘也必须抑制已保存会话。

    前端当前会发送无 session_id 的遗忘请求；同步入口此前只在
    `if req.session_id` 分支内做抑制，导致旧会话内容仍可恢复。
    """
    from api import server

    sid = session_store.create_session("nosession_forget")["id"]
    session_store.append_turn(
        "nosession_forget", sid,
        user_content="我的手机号是 13812345678",
        assistant_content="已记录。",
        referenced_memory_ids=["mem-phone-9"],
    )

    class _ForgetAgent(_FakeAgent):
        def chat(self, request):
            assert request.session_id is None
            return {
                "answer": "已处理遗忘指令。",
                "memory_write": {"status": "skipped"},
                "reset_history": True,
                "deleted_ids": ["mem-phone-9"],
                "workflow": {"status": "completed", "route": "forget"},
            }

    monkeypatch.setattr(server, "MemoryAgent", _ForgetAgent)
    response = client.post("/agent/chat", json={
        "user_id": "nosession_forget", "message": "忘掉我的手机号",
        # 故意不带 session_id
    })
    assert response.status_code == 200
    assert session_store.build_model_history("nosession_forget", sid) == [], (
        "无 session_id 的遗忘请求也必须抑制已保存会话"
    )


def test_stream_forget_suppresses_before_done(monkeypatch) -> None:
    """复核阻塞项 2：流式遗忘必须**先**抑制、**再**发 done。

    严格时序断言：在包装器 yield 出 done 的**同一迭代步**立即读取历史——
    若抑制在 finally 才执行，此刻仍能看到旧内容。
    """
    import asyncio

    from api.server import _record_session_stream, orchestrator
    from src.agent.service import ChatRequest as ChatReq, MemoryAgent as Agent

    sid = session_store.create_session("stream_forget")["id"]
    session_store.append_turn(
        "stream_forget", sid,
        user_content="我的工号是 998877",
        assistant_content="已记录。",
        referenced_memory_ids=["mem-emp-1"],
    )

    class _ForgetStreamModel:
        tools_enabled = False
        timeout = 60.0

        def complete_stream(self, messages, tools, timeout=None):
            yield {"kind": "token", "text": "已处理遗忘指令。"}
            yield {"kind": "message",
                   "message": {"role": "assistant", "content": "已处理遗忘指令。"}}

    class _ForgetAgent(Agent):
        def _execute(self, request):
            result = super()._execute(request)
            result["reset_history"] = True
            result["deleted_ids"] = ["mem-emp-1"]
            result["workflow"] = {**(result.get("workflow") or {}),
                                  "status": "completed", "route": "forget"}
            return result

    async def scenario():
        agent = _ForgetAgent(orchestrator, _ForgetStreamModel(), stream=True)
        req = ChatReq(user_id="stream_forget", message="忘掉我的工号", session_id=sid)
        history_at_done = None
        async for chunk in _record_session_stream(agent.chat_stream(req), req):
            if isinstance(chunk, str) and chunk.startswith("data: "):
                event = json.loads(chunk[len("data: "):].strip())
                if event.get("type") == "done":
                    # 收到 done 的这一迭代步就要能看到抑制结果
                    history_at_done = session_store.build_model_history(
                        "stream_forget", sid
                    )
        return history_at_done

    history = asyncio.run(scenario())
    assert history == [], (
        "done 终态下发时旧内容必须已不可恢复（抑制先于终态，不得留到 finally）"
    )


def test_late_turn_after_forget_is_not_recorded(tmp_store) -> None:
    """复核阻塞项 3：先召回、再遗忘、最后尝试落库 → 旧内容仍无法恢复。

    真实并发时序：请求 A 召回记忆 M 并开始生成；请求 B 遗忘 M（此时 A 的
    消息尚不存在，抑制扫不到）；A 完成后落库。墓碑必须挡住这一回合。
    """
    sid = tmp_store.create_session("late_user")["id"]

    # 1) 遗忘发生（墓碑写入，此刻无任何消息可抑制）
    result = tmp_store.forget_memories("late_user", ["mem-late-1"])
    assert result["messages_suppressed"] == 0
    assert result["forgotten_recorded"] == 1

    # 2) 迟到的回合尝试落库（关联已遗忘记忆）
    recorded = tmp_store.append_turn(
        "late_user", sid,
        user_content="之前问过的事",
        assistant_content="根据你的记录……（旧回答）",
        referenced_memory_ids=["mem-late-1"],
    )
    assert recorded is False, "关联已遗忘记忆的迟到回合必须被拒绝落库"
    assert tmp_store.build_model_history("late_user", sid) == []
    assert tmp_store.get_session("late_user", sid)["messages"] == []

    # 3) 不相关的新回合仍可正常落库
    assert tmp_store.append_turn(
        "late_user", sid, user_content="新问题", assistant_content="新回答",
    ) is True
    assert len(tmp_store.build_model_history("late_user", sid)) == 2

    # 4) 重启后墓碑仍生效
    reopened = SessionStore(tmp_store.path)
    assert reopened.append_turn(
        "late_user", sid, user_content="再次迟到", assistant_content="旧内容",
        referenced_memory_ids=["mem-late-1"],
    ) is False
    reopened.close()


def test_forget_cannot_interleave_between_tombstone_check_and_insert(tmp_store, monkeypatch) -> None:
    user_id = "concurrent_user"
    memory_id = "concurrent-memory"
    sid = tmp_store.create_session(user_id)["id"]
    other = SessionStore(tmp_store.path)
    other._conn.execute("PRAGMA busy_timeout = 0")
    checked = tmp_store.forgotten_ids

    def check_then_attempt_forget(user, ids):
        result = checked(user, ids)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            other.forget_memories(user_id, [memory_id])
        return result

    monkeypatch.setattr(tmp_store, "forgotten_ids", check_then_attempt_forget)
    try:
        assert tmp_store.append_turn(user_id, sid, user_content="q", assistant_content="old answer",
                                     referenced_memory_ids=[memory_id])
        assert other.forget_memories(user_id, [memory_id])["messages_suppressed"] == 2
        assert tmp_store.build_model_history(user_id, sid) == []
    finally:
        other.close()


@pytest.mark.parametrize("streaming", [False, True])
def test_forget_suppresses_follow_up_that_used_saved_history(monkeypatch, streaming) -> None:
    from api import server

    user_id = "history_reference_user"
    sid = session_store.create_session(user_id)["id"]
    session_store.append_turn(user_id, sid, user_content="路径？", assistant_content="旧路径",
                              referenced_memory_ids=["original-memory"])
    if streaming:
        _install_streaming_agent(monkeypatch, answer="沿用上一个路径")
    else:
        monkeypatch.setattr(server, "MemoryAgent", _FakeAgent)

    path = "/agent/chat/stream" if streaming else "/agent/chat"
    reply = client.post(path, json={"user_id": user_id, "session_id": sid,
                                   "message": "重复刚才的路径", "use_memory": False,
                                   "save_memory": False})
    assert reply.status_code == 200
    assert len(session_store.build_model_history(user_id, sid)) == 4
    assert session_store.forget_memories(user_id, ["original-memory"])["messages_suppressed"] == 4
    assert session_store.build_model_history(user_id, sid) == []


def test_forget_memories_is_idempotent(tmp_store) -> None:
    """forget_memories 幂等：重复遗忘不重复抑制、墓碑不重复计数。"""
    sid = tmp_store.create_session("idem_user")["id"]
    tmp_store.append_turn(
        "idem_user", sid, user_content="q", assistant_content="a",
        memory_id="mem-idem",
    )
    first = tmp_store.forget_memories("idem_user", ["mem-idem"])
    assert first == {"forgotten_recorded": 1, "messages_suppressed": 2}
    second = tmp_store.forget_memories("idem_user", ["mem-idem"])
    assert second == {"forgotten_recorded": 0, "messages_suppressed": 0}


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
