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


def test_dispatch_rejects_unknown_and_matches_direct_call(tmp_path, monkeypatch) -> None:
    """未知动作经 dispatch 结构化拒绝；合法动作与直接调用等价。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    (tmp_path / "f.txt").write_text("内容", encoding="utf-8")
    ctx = ActionContext(tools_enabled=True)
    ok, content, error = dispatch_action("not_a_tool", {}, ctx)
    assert ok is False and error == "tool_unavailable"
    ok_d, text_d, err_d = dispatch_action("read_file", {"path": "f.txt"}, ctx)
    ok_direct, text_direct = A._read_file("f.txt")
    assert ok_d == ok_direct is True and text_d == text_direct and err_d is None


def test_exec_terminal_direct_entry_requires_allowlist(tmp_path, monkeypatch) -> None:
    """直调 exec_terminal 仍受主机命令许可约束（安全边界不因目录放宽）。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    ctx = ActionContext(tools_enabled=True, exec_enabled=True)
    ok, content, error = dispatch_action(
        "exec_terminal", {"command": "git status"}, ctx)
    assert ok is False
    assert error == "tool_failed"  # 工具自身拒绝：(False, 说明) 兜底错误码
    assert "未获主机预先许可" in content


def test_search_memory_dispatch_returns_triple() -> None:
    """search_memory 经 dispatch 返回与其它动作一致的三元组（复审项 3）。"""
    ctx = ActionContext(
        tools_enabled=True, use_memory=True,
        search_memory=lambda query: [{"id": "m-1", "content": query, "score": 0.9}],
        memory_payload=lambda hits: (True, '[{"id": "m-1"}]'),
    )
    ok, content, error = dispatch_action("search_memory", {"query": "偏好"}, ctx)
    assert ok is True and error is None
    assert content == '[{"id": "m-1"}]'
    # 回调缺失时结构化拒绝，不抛 AssertionError 出破坏调用方解包
    ok2, _, error2 = dispatch_action(
        "search_memory", {"query": "偏好"},
        ActionContext(tools_enabled=True, use_memory=True))
    assert ok2 is False and error2 == "tool_unavailable"


# ---------------------------------------------------------------------------
# 桌面/API 直调入口（api/workspace.py）：与模型同目录、同开关、等价结果
# ---------------------------------------------------------------------------

def test_workspace_actions_api_equivalence_and_rejections(tmp_path, monkeypatch):
    """同一只读动作经 API 与 dispatch 等价；缺宿主凭据/跨工作区在执行前拒绝。"""
    from fastapi.testclient import TestClient
    from api import server

    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    monkeypatch.setenv("AGENT_ENABLE_WORKSPACE", "1")
    (tmp_path / "note.txt").write_text("动作目录正文", encoding="utf-8")

    with TestClient(server.app) as client:
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "")
        assert client.post("/workspace/actions/read_file", json={
            "arguments": {"path": "note.txt"},
        }).status_code == 401
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "test-host-token")
        assert client.post("/workspace/actions/read_file", json={
            "arguments": {"path": "note.txt"},
        }).status_code == 401
        client.headers["Authorization"] = "Bearer test-host-token"
        # 工具主开关关闭：与模型一致，动作 API 整体拒绝
        monkeypatch.setenv("AGENT_ENABLE_TOOLS", "0")
        listing = client.get("/workspace/actions").json()
        assert listing["enabled"] is False and listing["actions"] == []
        assert client.post("/workspace/actions/calculate",
                           json={"arguments": {"expression": "1+1"}}).status_code == 404

        # 开关开启：目录列出 9 个基础工具（无 search/exec/web/mutating）
        monkeypatch.setenv("AGENT_ENABLE_TOOLS", "1")
        names = {a["name"] for a in client.get("/workspace/actions").json()["actions"]}
        assert "calculate" in names and "read_file" in names
        assert "search_memory" not in names and "exec_terminal" not in names
        assert "write_file" not in names

        # 等价性：API 与 dispatch_action 直调同一动作，结果一致
        direct_ok, direct_text, _ = dispatch_action(
            "read_file", {"path": "note.txt"}, ActionContext(tools_enabled=True))
        resp = client.post("/workspace/actions/read_file",
                           json={"arguments": {"path": "note.txt"}})
        assert resp.status_code == 200 and direct_ok is True
        assert resp.json()["result"] == direct_text

        # 即使持有宿主凭据，也只能直调只读工具。
        assert client.post("/workspace/actions/write_file",
                           json={"arguments": {"path": "x.txt", "content": "y"}}).status_code == 403
        assert client.post("/workspace/actions/exec_terminal",
                           json={"arguments": {"command": "git status"}}).status_code == 403
        # search_memory 需要请求级记忆上下文 → 403
        assert client.post("/workspace/actions/search_memory",
                           json={"arguments": {"query": "x"}}).status_code == 403

        assert client.post("/workspace/actions/read_file", json={
            "user_id": "someone", "arguments": {"path": "note.txt"},
        }).status_code == 422

        for options in ({"context": {}}, {"glob": 5}, {"case_sensitive": "false"}):
            resp = client.post("/workspace/actions/search_content", json={
                "arguments": {"pattern": "动作目录", **options},
            })
            assert resp.status_code == 400
            assert resp.json()["error_code"] == "invalid_arguments"

        # 拒绝场景 2：跨工作区（沙箱外路径）在执行前拒绝 → 400 + error_code
        resp = client.post("/workspace/actions/read_file",
                           json={"arguments": {"path": "../../outside.txt"}})
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False and body["error_code"] in {"tool_error", "invalid_arguments", "tool_failed"}
        assert body["detail"], "跨工作区拒绝必须携带说明（如：路径受保护或超出工作区范围。）"

        # 未知动作 → 404
        assert client.post("/workspace/actions/rm_rf",
                           json={"arguments": {}}).status_code == 404
