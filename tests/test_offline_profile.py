"""TASK-025 / Issue #33 断网运行配置与能力清单回归。

对应验收：
- 网络阻断且无密钥时能列出功能、运行确定性动作
- 缺少模型明确降级；离线模式无非回环出站尝试
- 出站审计记录 DNS/连接尝试（目标类别 + 主机 + 结果，不含正文）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.agent import actions as A
from src.agent.actions import ActionContext, dispatch_action
from src.common import offline_profile as of


@pytest.fixture(autouse=True)
def clean_audit():
    of.reset_outbound_audit()
    yield
    of.reset_outbound_audit()


# ---------------------------------------------------------------------------
# 出站裁决与审计
# ---------------------------------------------------------------------------

def test_offline_mode_blocks_non_loopback_outbound(monkeypatch) -> None:
    """离线档：非回环出站在发起前拒绝；回环服务放行。"""
    monkeypatch.setenv("OS_AGENT_OFFLINE_MODE", "1")
    allowed, reason = of.outbound_allowed("web_fetch", "https://example.com/page")
    assert allowed is False and "离线" in (reason or "")
    allowed_loop, reason_loop = of.outbound_allowed("llm_chat", "http://127.0.0.1:1234/v1")
    assert allowed_loop is True and reason_loop is None  # 本地 LM Studio 不受阻

    audit = of.outbound_audit()
    assert len(audit) == 1
    assert audit[0]["category"] == "web_fetch"
    assert audit[0]["host"] == "example.com"
    assert audit[0]["allowed"] is False
    # 最小审计：无 URL 查询参数与正文
    assert "page" not in json.dumps(audit)


def test_unknown_category_rejected_and_audited_context(monkeypatch) -> None:
    """未登记的出站类别拒绝（新入口必须登记到 OUTBOUND_CATEGORIES）。"""
    allowed, reason = of.outbound_allowed("unknown_feed", "https://example.com")
    assert allowed is False and "未登记" in (reason or "")


def test_online_mode_allows_outbound(monkeypatch) -> None:
    """非离线档放行（安全防护另由 is_external_url_safe 承担）。"""
    monkeypatch.delenv("OS_AGENT_OFFLINE_MODE", raising=False)
    allowed, reason = of.outbound_allowed("web_fetch", "https://example.com")
    assert allowed is True and reason is None


def test_web_fetch_goes_through_offline_gate(monkeypatch, tmp_path) -> None:
    """web_fetch 实际执行时经离线裁决：断网档下不再发起连接。"""
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    monkeypatch.setenv("OS_AGENT_OFFLINE_MODE", "1")
    ctx = ActionContext(tools_enabled=True, web_enabled=True)
    ok, content, error = dispatch_action("web_fetch", {"url": "https://example.com"}, ctx)
    assert ok is False and "离线" in content
    assert any(e["category"] == "web_fetch" and e["allowed"] is False
               for e in of.outbound_audit())


# ---------------------------------------------------------------------------
# 能力清单：无密钥/断网降级语义
# ---------------------------------------------------------------------------

def test_capability_report_without_key_offline(monkeypatch) -> None:
    """断网 + 无密钥：聊天/网页/更新停用，偏好降级但规则可用，确定性动作列出。"""
    monkeypatch.setenv("OS_AGENT_OFFLINE_MODE", "1")
    monkeypatch.delenv("LM_STUDIO_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LM_STUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    report = of.capability_report()
    caps = report["capabilities"]
    assert report["offline_mode"] is True
    # 云聊天不可用且说明原因
    assert caps["chat"]["status"] == "unavailable"
    assert "离线" in caps["chat"]["detail"] or "密钥" in caps["chat"]["detail"]
    # 偏好降级但规则提取明确可用
    assert caps["preference"]["status"] == "degraded"
    assert "规则提取可用" in caps["preference"]["detail"]
    # 网页/更新停用；遥测占位；确定性动作可用
    assert caps["web"]["status"] == "unavailable"
    assert caps["update_check"]["status"] == "unavailable"
    assert caps["actions"]["status"] == "available"
    assert of.deterministic_actions_available(), "确定性动作清单非空"
    # 每项都有 status 与 detail（不写死、可渲染）
    for name, cap in caps.items():
        assert cap["status"] in {"available", "degraded", "unavailable"}, name
        assert cap["detail"], name


def test_capability_report_local_loopback_chat_available(monkeypatch) -> None:
    """显式配置的回环 LM Studio：离线档下聊天仍可用（本地模型档）。"""
    monkeypatch.setenv("OS_AGENT_OFFLINE_MODE", "1")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")  # 显式覆盖才算本地档
    report = of.capability_report()
    chat = report["capabilities"]["chat"]
    assert chat["status"] == "available" and "回环" in chat["detail"]
    assert report["profile"] in {"basic", "local_model"}


def test_embedding_degradation_reported(monkeypatch) -> None:
    """嵌入降级状态如实上报（不冒充模型可用）。"""
    monkeypatch.delenv("OS_AGENT_OFFLINE_MODE", raising=False)
    report = of.capability_report()
    embed = report["capabilities"]["embedding"]
    assert embed["status"] in {"available", "degraded"}
    assert "requirement" in embed and embed["requirement"]["model"]
    if embed["status"] == "degraded":
        assert "哈希" in embed["detail"] or "未安装" in embed["detail"]


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------

def test_capabilities_endpoint(monkeypatch) -> None:
    from api.server import app
    monkeypatch.delenv("OS_AGENT_OFFLINE_MODE", raising=False)
    with TestClient(app) as client:
        data = client.get("/agent/capabilities").json()
    assert data["offline_mode"] is False
    assert {"chat", "preference", "embedding", "memory", "voice", "web",
            "plugins", "telemetry", "update_check", "actions"} == set(data["capabilities"])


def test_deterministic_actions_work_offline(monkeypatch, tmp_path) -> None:
    """断网 + 无密钥：确定性动作经动作目录照常可用（验收场景）。"""
    monkeypatch.setenv("OS_AGENT_OFFLINE_MODE", "1")
    monkeypatch.setenv("AGENT_EXEC_CWD", str(tmp_path))
    (tmp_path / "note.txt").write_text("断网验证内容", encoding="utf-8")
    ctx = ActionContext(tools_enabled=True)
    for name, args in (
        ("get_current_datetime", {}),
        ("calculate", {"expression": "6*7"}),
        ("read_file", {"path": "note.txt"}),
    ):
        ok, content, error = dispatch_action(name, args, ctx)
        assert ok is True, f"{name} 断网必须可用：{content}"
        assert error is None
