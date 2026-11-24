"""TASK-021 统一动作目录回归：目录完整性、等价分发、能力开关与未知动作。

对应 Issue #29 验收：
- 同一只读动作经目录分发与直接调用返回等价结果；
- 未知动作、参数错误在执行前被结构化拒绝（tool_unavailable / invalid_arguments）；
- 能力开关（use_memory / exec / web / tools）与原 service.py 注册条件一致。
"""

from __future__ import annotations

import pytest

from src.agent import actions as A
from src.agent.actions import ActionContext, dispatch_action, tools_for_model


EXPECTED_NAMES = (
    "search_memory", "exec_terminal", "read_file", "list_directory",
    "get_current_datetime", "calculate", "search_files", "search_content",
    "write_file", "edit_file", "delete_file", "web_fetch",
)


def test_catalog_contains_all_legacy_tools_in_registration_order() -> None:
    """目录覆盖原 12 个工具，顺序与原 service.py 注册顺序一致。"""
    assert tuple(action.name for action in A.ACTIONS) == EXPECTED_NAMES
    assert len(A._ACTION_BY_NAME) == len(EXPECTED_NAMES)


@pytest.mark.parametrize("name,mutates,artifact", [
    ("search_memory", False, False),
    ("exec_terminal", True, False),
    ("read_file", False, False),
    ("list_directory", False, False),
    ("get_current_datetime", False, False),
    ("calculate", False, False),
    ("search_files", False, False),
    ("search_content", False, False),
    ("write_file", True, True),
    ("edit_file", True, True),
    ("delete_file", True, True),
    ("web_fetch", False, False),
])
def test_mutation_flags_match_legacy_dispatch(name: str, mutates: bool, artifact: bool) -> None:
    """变更/工件标记与原 _tools 的硬编码集合一致。"""
    action = A.get_action(name)
    assert action is not None
    assert action.mutates is mutates
    assert action.artifact is artifact
    assert (name in A.mutating_actions()) is mutates
    assert (name in A.artifact_actions()) is artifact


def test_dispatch_equivalent_to_direct_call(tmp_path, monkeypatch) -> None:
    """同一只读动作经 dispatch 与直接调用等价（Issue #29 验收）。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    ctx = ActionContext(tools_enabled=True)
    (tmp_path / "note.txt").write_text("目录正文", encoding="utf-8")

    ok_a, text_a = A._read_file("note.txt")
    ok_b, text_b, err_b = dispatch_action("read_file", {"path": "note.txt"}, ctx)
    assert ok_a == ok_b is True and text_a == text_b and err_b is None

    ok_a, text_a = A._calculate("1+2*3")
    ok_b, text_b, err_b = dispatch_action("calculate", {"expression": "1+2*3"}, ctx)
    assert ok_a == ok_b is True and text_a == text_b and err_b is None


def test_unknown_action_rejected_before_execution() -> None:
    """未知动作返回结构化 tool_unavailable，不抛异常。"""
    ok, content, error = dispatch_action("rm_rf", {}, ActionContext(tools_enabled=True))
    assert ok is False and error == "tool_unavailable"
    assert "不支持的工具" in content


def test_invalid_arguments_rejected_before_execution(tmp_path, monkeypatch) -> None:
    """参数错误（未知键）在执行前拒绝，返回 invalid_arguments。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    ok, content, error = dispatch_action(
        "read_file", {"path": "a.txt", "extra": 1}, ActionContext(tools_enabled=True))
    assert ok is False and error == "invalid_arguments"


def test_capability_gates_match_legacy_registration() -> None:
    """能力开关与原 _model 注册条件一致（search/exec/web 各自独立）。"""
    # 默认：主开关关闭 → 什么都没有
    assert tools_for_model(ActionContext()) == []
    # 主开关开 → 仅 9 个基础工具
    base = tools_for_model(ActionContext(tools_enabled=True))
    assert [schema["function"]["name"] for schema in base] == [
        "read_file", "list_directory", "get_current_datetime", "calculate",
        "search_files", "search_content", "write_file", "edit_file", "delete_file"]
    # +web
    web = tools_for_model(ActionContext(tools_enabled=True, web_enabled=True))
    assert web[-1]["function"]["name"] == "web_fetch" and len(web) == 10
    # +exec（位于基础工具之前）
    exe = tools_for_model(ActionContext(tools_enabled=True, exec_enabled=True))
    assert exe[0]["function"]["name"] == "exec_terminal" and len(exe) == 10
    # search_memory 需要 use_memory 且注入回调
    mem = tools_for_model(ActionContext(
        tools_enabled=True, use_memory=True,
        search_memory=lambda q: [], memory_payload=lambda hits: (True, "[]")))
    assert mem[0]["function"]["name"] == "search_memory" and len(mem) == 10


def test_dispatch_respects_capability_gates(tmp_path, monkeypatch) -> None:
    """分发端同样被能力开关拦截（未开启的动作不可经 UI/API 直调）。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    ok, _, error = dispatch_action("calculate", {"expression": "1"}, ActionContext())
    assert ok is False and error == "tool_unavailable"

    ok, _, error = dispatch_action(
        "search_memory", {"query": "x"},
        ActionContext(tools_enabled=True, use_memory=False))
    assert ok is False and error == "tool_unavailable"


def test_local_tool_wrapper_keeps_legacy_contract(tmp_path, monkeypatch) -> None:
    """service._run_local_tool 兼容包装：未知动作抛 ValueError，合法动作等价。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    (tmp_path / "f.txt").write_text("内容", encoding="utf-8")
    with pytest.raises(ValueError):
        A._run_local_tool("not_a_tool", {})
    ok_wrapper, text_wrapper = A._run_local_tool("read_file", {"path": "f.txt"})
    ok_direct, text_direct = A._read_file("f.txt")
    assert ok_wrapper == ok_direct is True and text_wrapper == text_direct


def test_exec_terminal_direct_entry_requires_allowlist(tmp_path, monkeypatch) -> None:
    """直调 exec_terminal 仍受主机命令许可约束（安全边界不因目录放宽）。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    ctx = ActionContext(tools_enabled=True, exec_enabled=True)
    ok, content, error = dispatch_action(
        "exec_terminal", {"command": "git status"}, ctx)
    assert ok is False
    assert error is None  # 工具自身返回 (False, 说明)，非参数错误
    assert "未获主机预先许可" in content
