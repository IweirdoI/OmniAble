"""TASK-020 / Issue #25 受控文件变更一次性确认回归（复核整改版）。

覆盖 Clerk 复核要求：
- 只对受控文件变更（write/edit/delete_file）挂闸门，只读计算/检索不被拦截
- 审批绑定规范相对路径、版本（含目标不存在）、run/tool call
- 携批准版本进入实际写入（CAS；批准后目标变化即作废）
- 取消与写入时序：等待中取消不写；批准与写入之间取消不写
- 可信宿主决定入口：决定端点必须携带 SSE 下发的 decision_token
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.agent import confirmations
from src.agent.actions import ActionContext, dispatch_action
from src.agent.service import (
    ChatRequest,
    MemoryAgent,
    _confirm_mutation,
)
from src.agent.workspace import Workspace
from src.agent.workflow import WorkflowBudget
from src.security.service import SecurityAndForgetService


class _Model:
    tools_enabled = True

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def complete(self, messages, tools=None, timeout=None):
        self.calls += 1
        return self.answers[min(self.calls, len(self.answers)) - 1]

    def complete_stream(self, messages, tools=None, timeout=None):
        def gen():
            yield {"kind": "message", "message": self.complete(messages, tools, timeout)}
        return gen()


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    monkeypatch.setenv("AGENT_ENABLE_WORKSPACE", "1")
    monkeypatch.setenv("AGENT_ENABLE_TOOLS", "1")
    monkeypatch.setenv("AGENT_REQUIRE_FILE_CONFIRM", "1")
    return tmp_path


def _memory():
    return Mock(spec=SecurityAndForgetService)


def _call(action: str, args: dict, call_id: str = "w1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": action, "arguments": json.dumps(args)}}


def _agent(calls, *, answer="已完成", stream: bool = False):
    return MemoryAgent(_memory(), _Model(
        {"tool_calls": calls}, {"content": answer},
    ), stream=stream, budget=WorkflowBudget(context_window_tokens=14000))


def _collect(agent, request):
    async def run():
        return [json.loads(frame[5:]) async for frame in
                agent.chat_stream(request) if frame.startswith("data:")]
    return asyncio.run(run())


def _events_of(events, kind):
    return [e for e in events if e.get("type") == kind]


# ---------------------------------------------------------------------------
# 只读动作不拦截（复核项 1）
# ---------------------------------------------------------------------------

def test_readonly_actions_not_intercepted_when_gate_on(ws_env, monkeypatch):
    """闸门开启时，只读计算在同步入口照常执行（不得被确认拦截）。"""
    agent = _agent([_call("calculate", {"expression": "2+3"})], answer="2+3 = 5")
    result = agent.chat(ChatRequest(user_id="synthetic", message="计算 2+3", use_memory=False))
    assert result["workflow"]["status"] == "completed"
    step = result["workflow"]["steps"][0]
    assert step["ok"] is True and step["error_code"] is None


def test_gate_off_keeps_legacy_behavior(ws_env, monkeypatch):
    monkeypatch.delenv("AGENT_REQUIRE_FILE_CONFIRM")
    agent = _agent([_call("write_file", {"path": "notes.md", "content": "草稿"})])
    result = agent.chat(ChatRequest(user_id="synthetic", message="生成笔记", use_memory=False))
    assert result["workflow"]["status"] == "completed"
    assert (ws_env / "notes.md").read_text(encoding="utf-8") == "草稿"


# ---------------------------------------------------------------------------
# 同步入口：文件变更 fail-closed
# ---------------------------------------------------------------------------

def test_sync_entry_rejects_file_mutation(ws_env):
    agent = _agent([_call("write_file", {"path": "notes.md", "content": "草稿"})])
    result = agent.chat(ChatRequest(user_id="synthetic", message="生成笔记", use_memory=False))
    assert result["workflow"]["status"] == "partial"
    step = result["workflow"]["steps"][0]
    assert step["ok"] is False and step["error_code"] == "confirmation_denied"
    assert not (ws_env / "notes.md").exists()
    assert "已完成" not in result["answer"]


# ---------------------------------------------------------------------------
# 流式入口：批准（携 token）/拒绝/超时/断线/取消时序
# ---------------------------------------------------------------------------

def _auto_decide_holder(agent, decision):
    """等待 agent._ctx 建立后，用请求自带的 decision_token 自动提交决定。"""
    stop = threading.Event()
    registry = confirmations.registry()

    def run():
        while not stop.is_set():
            ctx = agent._ctx
            if ctx is not None:
                with registry._lock:
                    entry = next((e for e in registry._entries.values()
                                  if e.request.run_id == ctx.run_id
                                  and e.decision is None), None)
                if entry is not None:
                    registry.decide(entry.request.confirm_id, decision,
                                    entry.request.decision_token)
                    return
            stop.wait(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop, thread


def test_stream_approve_writes_with_approved_revision(ws_env):
    """流式批准：token 决定 → 携批准版本 CAS 写入 → 文件落盘。"""
    agent = _agent([_call("write_file", {"path": "notes.md", "content": "草稿"})],
                   stream=True)
    stop, thread = _auto_decide_holder(agent, "approve")
    try:
        events = _collect(agent, ChatRequest(
            user_id="synthetic", message="生成笔记", use_memory=False))
    finally:
        stop.set()
    confirm = _events_of(events, "confirmation_request")
    assert confirm and confirm[0]["action"] == "write_file"
    assert confirm[0]["path"] == "notes.md"
    assert confirm[0]["target_exists"] is False and confirm[0]["expected_revision"] is None
    assert confirm[0]["decision_token"], "确认事件必须携带宿主 decision_token"
    assert any(event["id"] == confirm[0]["tool_call_id"] and event["ok"]
               for event in _events_of(events, "tool_result"))
    assert (ws_env / "notes.md").read_text(encoding="utf-8") == "草稿"


def test_stream_approve_delete_uses_approved_revision(ws_env):
    """批准版本进入删除（CAS）：文件存在且版本匹配才删除。"""
    workspace = Workspace(ws_env)
    workspace.write("old.md", "旧内容", None)
    agent = _agent([_call("delete_file", {"path": "old.md"})], stream=True)
    stop, thread = _auto_decide_holder(agent, "approve")
    try:
        events = _collect(agent, ChatRequest(
            user_id="synthetic", message="删除 old.md", use_memory=False))
    finally:
        stop.set()
    confirm = _events_of(events, "confirmation_request")
    assert confirm and confirm[0]["target_exists"] is True
    assert confirm[0]["expected_revision"]
    assert not (ws_env / "old.md").exists()


def test_approve_then_target_changed_voids_decision(ws_env):
    """批准后、执行前目标被第三方修改 → 决定作废（revision_changed）。"""
    workspace = Workspace(ws_env)
    workspace.write("notes.md", "原始", None)
    revision = workspace.read("notes.md")["revision"]
    request = confirmations.ConfirmationRequest(
        confirm_id="cfm-rv", run_id="run-rv", tool_call_id="w1", action="edit_file",
        path="notes.md", target_exists=True, expected_revision=revision,
        summary="文本替换：notes.md", decision_token="tok-rv")
    registry = confirmations.registry()
    registry.register(request)
    workspace.write("notes.md", "第三方", revision)  # 决定前文件被改
    registry.decide("cfm-rv", "approve", "tok-rv")
    ctx = SimpleNamespace(run_id="run-rv", cancelled=False, remaining_seconds=lambda: 30.0)
    outcome = _confirm_mutation(
        "edit_file", {"path": "notes.md", "old_string": "原始", "new_string": "新"},
        ctx, "w1", emit=lambda e: None, streaming=True, request=request)
    assert isinstance(outcome, tuple)
    assert outcome[0] is False and outcome[2] == "confirmation_revision_changed"
    assert (ws_env / "notes.md").read_text(encoding="utf-8") == "第三方"
    registry.drop_run(request.run_id)


def test_deny_blocks_and_cancel_before_write(ws_env):
    """拒绝不写；批准后、写入前取消 → 不写（取消与写入时序）。"""
    request = confirmations.ConfirmationRequest(
        confirm_id="cfm-dn", run_id="run-dn", tool_call_id="w1", action="write_file",
        path="c.txt", target_exists=False, expected_revision=None,
        summary="写入 3 字符：c.txt", decision_token="tok-dn")
    registry = confirmations.registry()
    registry.register(request)
    registry.decide("cfm-dn", "deny", "tok-dn")
    ctx = SimpleNamespace(run_id="run-dn", cancelled=False, remaining_seconds=lambda: 30.0)
    ok, content, error = _confirm_mutation(
        "write_file", {"path": "c.txt", "content": "abc"}, ctx, "w1",
        emit=lambda e: None, streaming=True, request=request)
    assert ok is False and error == "confirmation_denied"

    # 取消先于写入：批准后 ctx.cancelled=True → check_cancel 抛 RunAborted，不执行写入
    from src.agent.runtime import RunAborted, RunContext

    request2 = confirmations.ConfirmationRequest(
        confirm_id="cfm-cc", run_id="run-cc", tool_call_id="w1", action="write_file",
        path="d.txt", target_exists=False, expected_revision=None,
        summary="写入 3 字符：d.txt", decision_token="tok-cc")
    registry.register(request2)
    registry.decide("cfm-cc", "approve", "tok-cc")
    run_ctx = RunContext(route="tool_task")
    outcome = _confirm_mutation(
        "write_file", {"path": "d.txt", "content": "abc"}, run_ctx, "w1",
        emit=lambda e: None, streaming=True, request=request2)
    assert isinstance(outcome, confirmations.ConfirmationRequest)  # 闸门批准
    run_ctx.cancel("user_stop")
    with pytest.raises(RunAborted):
        dispatch_action("write_file", {"path": "d.txt", "content": "abc"}, ActionContext(
            tools_enabled=True, confirm_file_change=lambda *_: outcome,
            before_file_write=run_ctx.check))
    assert not (ws_env / "d.txt").exists()
    registry.drop_run(request.run_id)
    registry.drop_run(request2.run_id)


def test_timeout_and_disconnect(ws_env):
    request = confirmations.ConfirmationRequest(
        confirm_id="cfm-to", run_id="run-to", tool_call_id="w1", action="write_file",
        path="e.txt", target_exists=False, expected_revision=None,
        summary="写入 3 字符：e.txt", decision_token="tok-to")
    confirmations.registry().register(request)
    ctx = SimpleNamespace(run_id="run-to", cancelled=False, remaining_seconds=lambda: 0.4)
    ok, _, error = _confirm_mutation(
        "write_file", {"path": "e.txt", "content": "abc"}, ctx, "w1",
        emit=lambda e: None, streaming=True, request=request)
    assert ok is False and error == "confirmation_timeout"
    assert confirmations.registry().decide(request.confirm_id, "approve", request.decision_token) == (None, "unknown")

    request2 = confirmations.ConfirmationRequest(
        confirm_id="cfm-dc", run_id="run-dc", tool_call_id="w1", action="write_file",
        path="f.txt", target_exists=False, expected_revision=None,
        summary="写入 3 字符：f.txt", decision_token="tok-dc")
    confirmations.registry().register(request2)
    ctx_dc = SimpleNamespace(run_id="run-dc", cancelled=True, remaining_seconds=lambda: 30.0)
    ok, _, error = _confirm_mutation(
        "write_file", {"path": "f.txt", "content": "abc"}, ctx_dc, "w1",
        emit=lambda e: None, streaming=True, request=request2)
    assert ok is False and error == "confirmation_disconnected"


# ---------------------------------------------------------------------------
# 可信宿主决定入口（复核项 4）
# ---------------------------------------------------------------------------

def test_registry_decide_requires_matching_token():
    registry = confirmations.ConfirmationRegistry()
    request = confirmations.ConfirmationRequest(
        confirm_id="cfm-tk", run_id="run-tk", tool_call_id="w1", action="write_file",
        path="a.txt", target_exists=False, expected_revision=None,
        summary="写入", decision_token="good-token")
    registry.register(request)
    assert registry.decide("cfm-tk", "approve", "bad-token") == (None, confirmations.BAD_TOKEN)
    payload, error = registry.decide("cfm-tk", "approve", "good-token")
    assert error is None and payload["decision"] == "approve" and payload["duplicate"] is False
    payload2, error2 = registry.decide("cfm-tk", "deny", "good-token")
    assert error2 is None and payload2["decision"] == "approve" and payload2["duplicate"] is True
    assert registry.decide("no-such", "approve", "x") == (None, "unknown")


def test_decision_endpoint_token_required():
    from api.server import app
    with TestClient(app) as client:
        # 缺 token → 422（字段必填）
        assert client.post("/agent/confirmations/nope",
                           json={"decision": "approve"}).status_code == 422
        # 未知 id → 404
        assert client.post("/agent/confirmations/nope",
                           json={"decision": "approve", "token": "x"}).status_code == 404
        # 错误 token → 403；正确 token → 200
        request = confirmations.ConfirmationRequest(
            confirm_id="cfm-api", run_id="run-api", tool_call_id="w1", action="write_file",
            path="a.txt", target_exists=False, expected_revision=None,
            summary="写入", decision_token="secret-token")
        confirmations.registry().register(request)
        assert client.post("/agent/confirmations/cfm-api",
                           json={"decision": "approve", "token": "wrong"}).status_code == 403
        assert client.post("/agent/confirmations/cfm-api",
                           json={"decision": "approve", "token": "无效"}).status_code == 403
        assert client.post("/agent/confirmations/cfm-api",
                           json={"decision": "approve", "token": "secret-token"}).status_code == 200
        confirmations.registry().drop_run(request.run_id)


def test_late_decision_after_run_cannot_execute(ws_env):
    request = confirmations.ConfirmationRequest(
        confirm_id="cfm-late", run_id="run-late", tool_call_id="w1", action="write_file",
        path="late.txt", target_exists=False, expected_revision=None,
        summary="写入", decision_token="tok-late")
    registry = confirmations.registry()
    registry.register(request)
    assert registry.drop_run("run-late") == 1
    from api.server import app
    with TestClient(app) as client:
        resp = client.post("/agent/confirmations/cfm-late",
                           json={"decision": "approve", "token": "tok-late"})
    assert resp.status_code == 404
    assert not (ws_env / "late.txt").exists()


def test_execute_confirmed_write_rejects_when_target_appeared(ws_env):
    """批准时目标不存在、执行时出现 → CAS（expected=None）拒绝覆盖。"""
    (ws_env / "race.txt").write_text("第三方抢先写入", encoding="utf-8")
    request = confirmations.ConfirmationRequest(
        confirm_id="cfm-race", run_id="run-race", tool_call_id="w1", action="write_file",
        path="race.txt", target_exists=False, expected_revision=None,
        summary="写入", decision_token="tok-race")
    ok, _, error = dispatch_action(
        "write_file", {"path": "race.txt", "content": "模型草稿", "overwrite": True},
        ActionContext(tools_enabled=True, confirm_file_change=lambda *_: request))
    assert ok is False and error == confirmations.REVISION_CHANGED
    assert (ws_env / "race.txt").read_text(encoding="utf-8") == "第三方抢先写入"


def test_action_authorization_and_validation_precede_confirmation(ws_env):
    confirm = Mock()
    args = {"path": "notes.md", "content": "草稿"}
    assert dispatch_action("write_file", args, ActionContext(confirm_file_change=confirm))[2] == "tool_unavailable"
    assert dispatch_action("write_file", {**args, "extra": True}, ActionContext(
        tools_enabled=True, confirm_file_change=confirm))[2] == "invalid_arguments"
    confirm.assert_not_called()
    assert not (ws_env / "notes.md").exists()
