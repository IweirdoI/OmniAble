"""Exercise the actual requests delivered by the Agent to a synthetic model."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from src.agent.context import (
    HISTORY_WARNING, MEMORY_PREFIX, MEMORY_WARNING, RESPONSE_TOKENS, TOOL_WARNING,
    ContextLimitExceeded, estimate_input_tokens, pack_model_input,
)
from src.agent.service import ChatModel, ChatRequest, MemoryAgent
from src.agent.workflow import WorkflowBudget
from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.orchestrator import MemoryOrchestrator
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient


def tool_call(ident, args):
    return {"id": ident, "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps(args, ensure_ascii=False),
    }}


def model(*answers, tools=False):
    llm = Mock(tools_enabled=tools, timeout=2)
    llm.complete.side_effect = list(answers) or [{"content": "已回答"}]
    return llm


def test_long_chinese_history_retains_current_and_recent_complete_turn():
    budget = WorkflowBudget(context_window_tokens=9000)
    llm = model()
    messages = [{"role": role, "content": "旧轮次" + "汉" * 2700}
                for _ in range(8) for role in ("user", "assistant")]
    messages += [{"role": "user", "content": "最近问题"}, {"role": "assistant", "content": "最近回答"}]
    memory = Mock()
    reply = MemoryAgent(memory, llm, budget=budget).chat(ChatRequest(
        user_id="synthetic", message="当前请求必须原样保留", history=messages,
        use_memory=False, save_memory=False,
    ))
    sent, tools = llm.complete.call_args.args
    assert [row["content"] for row in sent[1:]] == ["最近问题", "最近回答", "当前请求必须原样保留"]
    assert estimate_input_tokens(sent, tools or ()) + RESPONSE_TOKENS <= budget.context_window_tokens
    assert reply["context_warnings"] == [HISTORY_WARNING]
    assert MEMORY_PREFIX not in str(sent)
    memory.recall.assert_not_called()
    memory.get_preference_profile.assert_not_called()
    memory.process_input.assert_not_called()


def test_model_receives_authoritative_scene_preference_and_compact_knowledge(monkeypatch):
    storage = InMemoryMilvusClient()
    for ident, value, scene, version, latest in (
        ("old", "旧下载设置", "", 1, False),
        ("global", "全局新设置", "", 2, True),
        ("office", "办公新设置", "office", 3, True),
        ("other", "其他场景设置", "personal", 4, True),
    ):
        storage.insert(ident, value, [0.0], {"user_id": "synthetic", "memory_type": "preference",
            "preference_key": "habit.download_path", "preference_value": value, "scene_id": scene,
            "scope": "scene" if scene else "global", "version": version, "is_latest": latest})
    memory = MemoryOrchestrator(milvus=storage)
    text = "部署资料" + "中" * 1000
    records = [
        MemoryItem(id="old", user_id="synthetic", content="旧下载设置", layer=MemoryLayer.LONG,
                   memory_type=MemoryType.PREFERENCE, metadata={"preference_key": "habit.download_path"}),
        MemoryItem(id="doc", user_id="synthetic", content=text, layer=MemoryLayer.LONG,
                   memory_type=MemoryType.KNOWLEDGE,
                   metadata={"source_type": "document", "filename": "C:/private/guide.md", "extra": "unused"}),
        MemoryItem(id="duplicate", user_id="synthetic", content=text, layer=MemoryLayer.LONG,
                   memory_type=MemoryType.KNOWLEDGE),
    ]
    monkeypatch.setattr(memory, "recall", lambda query: SimpleNamespace(items=records))
    for scene, expected, preference_id in (
        (None, "全局新设置", "global"), ("office", "办公新设置", "office"),
    ):
        llm = model()
        result = MemoryAgent(memory, llm).chat(ChatRequest(
            user_id="synthetic", message="解释下载设置和部署资料", scene_id=scene,
            save_memory=False, session_id="session",
        ))
        messages, _ = llm.complete.call_args.args
        reference = next(row["content"] for row in messages if row["content"].startswith(MEMORY_PREFIX))
        payload = json.loads(reference.removeprefix(MEMORY_PREFIX))
        assert [row["value"] for row in payload["preferences"]] == [expected]
        assert "source_memory_id" not in payload["preferences"][0]
        assert result["referenced_memory_ids"] == sorted(["doc", preference_id])
        assert [row["memory_id"] for row in payload["memories"]] == ["doc"]
        assert payload["memories"][0]["filename"] == "guide.md"
        assert "private" not in reference and "unused" not in reference and "旧下载设置" not in reference
        assert MEMORY_WARNING in result["context_warnings"]
        assert len(result["memories"]) == 3  # UI recall evidence remains the original response contract.


def test_memory_lookup_in_tool_round_is_linked_to_saved_turn():
    lookup = tool_call("memory-lookup", {"query": "补查"})
    lookup["function"]["name"] = "search_memory"
    llm = model({"tool_calls": [lookup]}, {"content": "使用补查资料"}, tools=True)
    memory = Mock()
    memory.get_preference_profile.return_value = {"features": []}
    hit = MemoryItem(id="tool-only", user_id="synthetic", content="补查资料",
                     layer=MemoryLayer.LONG, memory_type=MemoryType.KNOWLEDGE)
    memory.recall.side_effect = [SimpleNamespace(items=[]), SimpleNamespace(items=[hit])]

    result = MemoryAgent(memory, llm).chat(ChatRequest(
        user_id="synthetic", message="帮我查资料", session_id="session", save_memory=False,
    ))
    assert result["referenced_memory_ids"] == ["tool-only"]


def test_tool_rounds_can_drop_an_entire_old_batch_and_keep_latest_pairing():
    budget = WorkflowBudget(context_window_tokens=4800, tool_result_tokens=1700)
    state = [{"role": "system", "content": "系统"}, {"role": "user", "content": "当前请求"}]
    for prefix in ("old", "latest"):
        state += [{"role": "assistant", "content": "", "tool_calls": [tool_call(prefix, {"path": "readme"})]},
                  {"role": "tool", "tool_call_id": prefix, "content": "资料" * 1000}]
    sent, warnings = pack_model_input(state, 0, [], [], budget)
    assert [row["role"] for row in sent] == ["system", "user", "assistant", "tool"]
    assert sent[-2]["tool_calls"][0]["id"] == sent[-1]["tool_call_id"] == "latest"
    assert sent[-1]["content"].endswith("（工具输出已截断）")
    assert warnings == [TOOL_WARNING]
    assert estimate_input_tokens(sent) + RESPONSE_TOKENS <= budget.context_window_tokens


def test_actual_tool_payload_and_warnings_reach_stream_terminal(monkeypatch):
    import src.agent.service as service

    budget = WorkflowBudget(tool_result_tokens=900)
    llm = model({"tool_calls": [tool_call("read-1", {"path": "guide.txt"})]}, {"content": "已回答"}, tools=True)
    memory = Mock()
    monkeypatch.setattr(service, "_run_local_tool", lambda name, args: (True, "资料" * 5000))
    llm.complete_stream.side_effect = lambda *args, **kwargs: iter([{
        "kind": "message", "message": llm.complete(*args, **kwargs),
    }])
    agent = MemoryAgent(memory, llm, stream=True, budget=budget)

    async def collect():
        request = ChatRequest(user_id="synthetic", message="读取指南", use_memory=False, save_memory=False)
        return [json.loads(frame[5:]) async for frame in agent.chat_stream(request) if frame.startswith("data:")]

    events = asyncio.run(collect())
    for request in llm.complete.call_args_list:
        messages, tools = request.args
        assert estimate_input_tokens(messages, tools or ()) + RESPONSE_TOKENS <= budget.context_window_tokens
    sent = llm.complete.call_args.args[0]
    assert sent[-2]["tool_calls"][0]["id"] == sent[-1]["tool_call_id"] == "read-1"
    assert len(sent[-1]["content"].encode("utf-8")) <= 900
    assert events[-1]["type"] == "done" and TOOL_WARNING in events[-1]["context_warnings"]


def test_current_input_that_cannot_fit_returns_422_and_structured_stream_error(monkeypatch):
    import api.server as server

    llm = model()
    monkeypatch.setattr(server, "MemoryAgent", lambda memory, stream=False: MemoryAgent(
        Mock(), llm, stream=stream, budget=WorkflowBudget(context_window_tokens=4096)))
    payload = {"user_id": "synthetic", "message": "当前输入" * 1100, "use_memory": False}
    with TestClient(server.app) as client:
        response = client.post("/agent/chat", json=payload)
        events = client.post("/agent/chat/stream", json=payload)
    assert response.status_code == 422
    assert response.json()["workflow"]["stop_reason"] == "context_budget"
    terminal = [json.loads(line[5:]) for line in events.text.splitlines() if line.startswith("data:")][-1]
    assert terminal["type"] == "error" and terminal["workflow"]["stop_reason"] == "context_budget"
    llm.complete.assert_not_called()
    llm.complete_stream.assert_not_called()


def test_transport_checks_final_payload_before_opening_a_connection():
    llm = ChatModel(context_window_tokens=4096)
    messages = [{"role": "user", "content": "较长请求" * 500}]
    with pytest.raises(ContextLimitExceeded):
        llm._complete_once(messages, None, None)
    with pytest.raises(ContextLimitExceeded):
        list(llm._stream_once(messages, None, None))


def test_oversized_tool_request_ends_partial_without_an_unpaired_model_call(monkeypatch):
    import src.agent.service as service

    llm = model({"tool_calls": [tool_call("read-1", {"path": "文" * 6000})]}, tools=True)
    memory = Mock()
    monkeypatch.setattr(service, "_run_local_tool", lambda name, args: (True, "已读"))
    result = MemoryAgent(memory, llm, budget=WorkflowBudget(context_window_tokens=14000)).chat(ChatRequest(
        user_id="synthetic", message="读取工作区指南", use_memory=False,
    ))
    llm.complete.assert_called_once()
    memory.process_input.assert_not_called()
    assert result["workflow"]["status"] == "partial"
    assert result["workflow"]["stop_reason"] == "context_budget"
