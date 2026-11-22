"""Workflow control/evidence tests: synthetic users, files and models only."""
import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agent.runtime import EventChannel, RunAborted, RunContext, RunTimedOut, StreamSlots
from src.agent.service import ChatRequest, MemoryAgent
from src.agent.workflow import WorkflowBudget, route_request, load_workflow_budget


def call(name, args, ident="c1"):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Model:
    tools_enabled = True
    timeout = 1

    def __init__(self, *answers):
        self.answers = iter(answers or [{"content": "回答"}])
        self.seen = []

    def complete(self, messages, tools=None, timeout=None):
        self.seen.append(messages)
        answer = next(self.answers)
        return answer(messages) if callable(answer) else answer

    def complete_stream(self, messages, tools=None, timeout=None):
        answer = self.complete(messages, tools, timeout)
        if answer.get("content"):
            yield {"kind": "token", "text": answer["content"]}
        yield {"kind": "message", "message": answer}


@pytest.fixture
def memory():
    m = Mock()
    m.recall.return_value = SimpleNamespace(items=[])
    m.get_preference_profile.return_value = {}
    m.process_input.return_value = SimpleNamespace(accepted=True, memory_id="m1", reasons=[], memory_type="preference")
    m.forget.return_value = ["m1"]
    return m


def request(message="你好", **kwargs):
    return ChatRequest(user_id="synthetic", message=message, **kwargs)


async def collect(agent, req, **kwargs):
    return [json.loads(frame[5:]) async for frame in agent.chat_stream(req, **kwargs) if frame.startswith("data:")]


@pytest.mark.parametrize("text,route", [
    ("你好", "chat"), ("不要删除我的偏好", "chat"), ("解释一下忘记这个词", "chat"),
    ('“删除偏好”是什么意思', "chat"), ("请忘掉关于 Java 的偏好", "forget"),
    ("忘掉那个，再帮我写一个报告", "clarification"), ("删除", "clarification"),
    ("忘掉那个", "clarification"), ("删除关于 Java 的偏好吗？", "clarification"),
    ("读取两份文件并生成笔记", "tool_task"),
])
def test_routes(text, route):
    assert route_request(text) == route


def test_delete_file_is_verified_and_does_not_forget_memory(memory, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_EXEC_CWD', str(tmp_path))
    (tmp_path / 'remove.txt').write_text('synthetic', encoding='utf-8')
    model = Model({'tool_calls': [call('delete_file', {'path': 'remove.txt'})]}, {'content': '文件已删除'})
    result = MemoryAgent(memory, model).chat(request('删除 remove.txt', save_memory=False))
    assert result['workflow']['route'] == 'tool_task'
    assert result['workflow']['status'] == 'completed'
    assert result['workflow']['artifacts'] == [{'path': 'remove.txt', 'deleted': True}]
    assert not (tmp_path / 'remove.txt').exists()
    memory.forget.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_forget_and_clarification_share_routes(memory, stream):
    for text, expected in [("请忘掉关于 Java 的偏好", "completed"), ("忘掉那个，再帮我写一个报告", "needs_clarification")]:
        model = Model()
        a = MemoryAgent(memory, model, stream=stream)
        result = asyncio.run(collect(a, request(text)))[-1] if stream else a.chat(request(text))
        assert result["workflow"]["status"] == expected
        assert result["reset_history"] == (expected == "completed")
        assert not model.seen
    memory.forget.assert_called_once_with("synthetic", "请忘掉关于 Java 的偏好")
    memory.process_input.assert_not_called()


def test_real_file_evidence_and_no_generated_memory(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    (tmp_path / "a.txt").write_text("Alpha课程", encoding="utf-8")
    (tmp_path / "b.txt").write_text("Beta课程", encoding="utf-8")
    def write(messages):
        assert "Alpha课程" in str(messages) and "Beta课程" in str(messages)
        return {"tool_calls": [call("write_file", {"path": "notes.md", "content": "# 笔记\nAlpha课程\nBeta课程"}, "write")]}
    model = Model({"tool_calls": [call("read_file", {"path": "a.txt"}), call("read_file", {"path": "b.txt"}, "c2")]}, write, {"content": "已生成笔记"})
    result = MemoryAgent(memory, model).chat(request("读取两份文件并生成笔记", scene_id="class"))
    assert result["workflow"]["status"] == "completed"
    assert result["workflow"]["budget_used"]["tool_calls"] == 3
    assert result["workflow"]["budget_used"]["tool_rounds"] == 2
    assert result["workflow"]["artifacts"][0]["path"] == "notes.md"
    assert "Alpha课程" in (tmp_path / "notes.md").read_text(encoding="utf-8")
    assert memory.process_input.call_args.args[0] == {"content": "读取两份文件并生成笔记", "role": "user"}
    assert memory.process_input.call_args.kwargs["scene_id"] == "class"


def test_failure_cannot_become_completed_or_saved(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    result = MemoryAgent(memory, Model({"tool_calls": [call("read_file", {"path": "missing.txt"})]}, {"content": "全部成功了"})).chat(request("读取文件"))
    assert result["workflow"]["status"] == "partial"
    assert result["workflow"]["steps"][0]["ok"] is False
    assert "全部成功" not in result["answer"]
    memory.process_input.assert_not_called()


def test_duplicate_mutation_is_not_reexecuted(memory, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    calls = [call("write_file", {"path": "out.txt", "content": "one"}, ident) for ident in ("one", "two")]
    result = MemoryAgent(memory, Model({"tool_calls": calls}, {"content": "完成"})).chat(request("生成文件"))
    assert (tmp_path / "out.txt").read_text() == "one"
    assert result["workflow"]["steps"][1]["error_code"] == "duplicate_mutation"
    assert result["workflow"]["steps"][1]["executed"] is False
    assert result["workflow"]["budget_used"]["tool_calls"] == 1
    assert result["workflow"]["status"] == "partial"


@pytest.mark.parametrize("kind", ["calls", "failures", "rounds"])
def test_budgets_stop_actual_execution(memory, monkeypatch, kind):
    import src.agent.service as service
    operation = Mock(return_value=(kind != "failures", "result"))
    monkeypatch.setattr(service, "_run_local_tool", operation)
    budget = WorkflowBudget(max_total_tool_calls=1) if kind == "calls" else WorkflowBudget(max_consecutive_tool_failures=1) if kind == "failures" else WorkflowBudget(max_tool_rounds=1)
    calls = [call("calculate", {"expression": "1+1"}, ident) for ident in ("one", "two")]
    answers = [{"tool_calls": calls}, {"content": "回答"}] if kind != "rounds" else [{"tool_calls": calls[:1]}, {"tool_calls": calls}]
    result = MemoryAgent(memory, Model(*answers), budget=budget).chat(request())
    assert result["workflow"]["status"] == "partial"
    assert operation.call_count == 1
    assert result["workflow"]["budget_used"]["tool_calls"] == 1
    memory.process_input.assert_not_called()


def test_disabled_memory_rejects_forged_search(memory):
    model = Model({"tool_calls": [call("search_memory", {"query": "secret"})]}, {"content": "无可用记忆"})
    result = MemoryAgent(memory, model).chat(request(use_memory=False, save_memory=False))
    memory.recall.assert_not_called()
    memory.get_preference_profile.assert_not_called()
    assert result["workflow"]["status"] == "partial"


def test_memory_failure_keeps_answer_and_marks_write(memory):
    memory.process_input.side_effect = OSError("synthetic")
    result = MemoryAgent(memory, Model()).chat(request())
    assert result["answer"] == "回答"
    assert result["workflow"]["status"] == "completed"
    assert result["memory_write"]["status"] == "failed" and result["warnings"]


def test_cancel_write_order_and_deadline():
    ctx = RunContext(); ctx.cancel()
    assert ctx.begin_write() is False
    ctx = RunContext(); assert ctx.begin_write()
    ctx.cancel(); assert ctx.write_started
    ctx = RunContext(clock=lambda: 1)
    ctx.deadline = 0
    with pytest.raises(RunTimedOut): ctx.begin_write()


def test_disconnect_returns_without_blocking_and_holds_slot(memory):
    started, release = threading.Event(), threading.Event()
    class Blocking(Model):
        def complete_stream(self, *args, **kwargs):
            started.set(); release.wait(3)
            yield {"kind": "message", "message": {"content": "late"}}
    slots = StreamSlots(1); assert slots.try_acquire()
    async def scenario():
        agent = MemoryAgent(memory, Blocking(), stream=True)
        gen = agent.chat_stream(request(), on_finished=slots.release)
        await anext(gen)
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(.005)
        assert started.is_set()
        before = time.monotonic(); await gen.aclose()
        assert time.monotonic()-before < .2
        assert slots.try_acquire() is False
        release.set()
        for _ in range(100):
            if slots.try_acquire():
                slots.release(); return
            await asyncio.sleep(.01)
        pytest.fail("worker slot was not released")
    try: asyncio.run(scenario())
    finally: release.set()
    memory.process_input.assert_not_called()


def test_deadline_ends_sse_while_blocked_worker_retains_slot(memory):
    gate = threading.Event(); slots = StreamSlots(1); slots.try_acquire()
    class Blocking(Model):
        def complete_stream(self, *args, **kwargs):
            gate.wait(2)
            yield {"kind": "message", "message": {"content": "late"}}
    agent = MemoryAgent(memory, Blocking(), stream=True, budget=replace(WorkflowBudget(), deadline_seconds=.08))
    try:
        events = asyncio.run(collect(agent, request(), on_finished=slots.release))
        assert events[-1]["type"] == "error"
        assert events[-1]["workflow"]["status"] == "timed_out"
        assert not slots.try_acquire()
    finally: gate.set()
    memory.process_input.assert_not_called()


def test_full_queue_cancels_producer_and_unique_terminal():
    ctx = RunContext(budget=replace(WorkflowBudget(), stream_queue_capacity=1, queue_put_patience_seconds=.02))
    channel = EventChannel(ctx); channel.put({"type": "token", "text": "first"})
    with pytest.raises(RunAborted): channel.put({"type": "token", "text": "second"})
    channel.get_nowait()
    assert channel.emit_terminal({"type": "done"})
    assert not channel.emit_terminal({"type": "error"})
    assert channel.get_nowait()["seq"] > 1


def test_stream_errors_are_safe_and_final_answer_is_authoritative(memory, tmp_path):
    class Broken(Model):
        def complete_stream(self, *args, **kwargs):
            yield {"kind": "token", "text": "partial"}
            raise OSError(f"{tmp_path / 'private-model.txt'} token=synthetic")
    events = asyncio.run(collect(MemoryAgent(memory, Broken(), stream=True), request()))
    assert events[-1]["type"] == "error"
    assert "private-model.txt" not in json.dumps(events)
    memory.process_input.assert_not_called()


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_invalid_integer_budget_rejected(monkeypatch, value):
    from src.agent import workflow
    monkeypatch.setattr(workflow.app_config, "get_section", lambda _: {"workflow": {"max_tool_rounds": value}})
    load_workflow_budget.cache_clear()
    try:
        with pytest.raises(ValueError): load_workflow_budget()
    finally: load_workflow_budget.cache_clear()


def test_admission_rejects_before_sse_headers(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    slots = StreamSlots(1); slots.try_acquire()
    monkeypatch.setattr(server, "get_stream_slots", lambda: slots)
    response = TestClient(server.app).post("/agent/chat/stream", json={"user_id": "synthetic", "message": "hello"})
    assert response.status_code == 429 and response.headers["retry-after"] == "2"
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_pending_terminal_does_not_hold_lock_while_queue_full():
    ctx = RunContext(budget=replace(WorkflowBudget(), stream_queue_capacity=1, queue_put_patience_seconds=.1))
    channel = EventChannel(ctx); channel.put({"type": "token"})
    worker = threading.Thread(target=lambda: channel.emit_terminal({"type": "done"}))
    worker.start()
    try:
        for _ in range(100):
            if channel.terminal_sent: break
            time.sleep(.001)
        before = time.monotonic()
        assert channel.finish_now({"type": "error"}) is None
        assert time.monotonic()-before < .05
        channel.get_nowait()
    finally: worker.join(1)
    assert not worker.is_alive()
    assert channel.get_nowait()["type"] == "done"


def test_write_already_started_reports_actual_result_after_disconnect(memory):
    started, gate, exited = threading.Event(), threading.Event(), threading.Event()
    def save(*args, **kwargs):
        started.set(); gate.wait(2)
        return SimpleNamespace(accepted=True, memory_id="actual", reasons=[], memory_type="preference")
    memory.process_input.side_effect = save
    agent = MemoryAgent(memory, Model(), stream=True)
    async def scenario():
        gen = agent.chat_stream(request(), on_finished=exited.set)
        await anext(gen)
        for _ in range(100):
            if started.is_set(): break
            await asyncio.sleep(.005)
        assert started.is_set()
        await gen.aclose()
        assert agent._ctx.write_started
        gate.set()
        for _ in range(100):
            if exited.is_set(): break
            await asyncio.sleep(.005)
        assert exited.is_set()
        assert agent._ctx.write_result["memory_id"] == "actual"
    try: asyncio.run(scenario())
    finally: gate.set()


def test_nonzero_exit_does_not_count_as_success(memory, monkeypatch):
    import sys
    command = f'"{sys.executable}" -c "raise SystemExit(3)"'
    monkeypatch.setenv("AGENT_ENABLE_EXEC_TOOL", "1")
    monkeypatch.setenv("AGENT_EXEC_ALLOWED_COMMANDS", json.dumps([command]))
    result = MemoryAgent(memory, Model({"tool_calls": [call("exec_terminal", {"command": command})]}, {"content": "成功"})).chat(request())
    assert result["workflow"]["status"] == "partial"
    assert result["workflow"]["steps"][0]["ok"] is False
    memory.process_input.assert_not_called()


def test_parallel_users_do_not_share_context(memory):
    from concurrent.futures import ThreadPoolExecutor
    one = MemoryAgent(memory, Model())
    two = MemoryAgent(memory, Model())
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(agent.chat, ChatRequest(user_id=user, message="你好", scene_id=scene, save_memory=False))
                   for agent, user, scene in [(one, "a", "office"), (two, "b", "coding")]]
        results = [future.result() for future in futures]
    assert results[0]["workflow"]["run_id"] != results[1]["workflow"]["run_id"]
    queries = memory.recall.call_args_list
    assert {(c.args[0].user_id, c.args[0].scene_id) for c in queries} == {("a", "office"), ("b", "coding")}


@pytest.mark.parametrize("timeout", [False, True])
def test_sync_http_errors_keep_status_and_run_evidence(memory, monkeypatch, timeout):
    from fastapi.testclient import TestClient
    import api.server as server
    from src.agent.service import ModelUnavailable
    def answer(messages):
        if timeout:
            time.sleep(.03)
            return {"content": "late"}
        raise ModelUnavailable("synthetic-offline")
    agent = MemoryAgent(memory, Model(answer), budget=replace(WorkflowBudget(), deadline_seconds=.01 if timeout else 1))
    monkeypatch.setattr(server, "MemoryAgent", lambda *args, **kwargs: agent)
    response = TestClient(server.app).post("/agent/chat", json={"user_id": "synthetic", "message": "hello"})
    assert response.status_code == (504 if timeout else 503)
    payload = response.json()
    workflow = payload["detail"]["workflow"] if timeout else payload["workflow"]
    assert workflow["status"] == ("timed_out" if timeout else "failed")
    memory.process_input.assert_not_called()
