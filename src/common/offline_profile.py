"""Windows 断网运行配置与能力清单（TASK-025 / Issue #33）。

把既有的降级行为显式化、可查询、有证据：

- `offline_mode()`：断网/无密钥运行档（`OS_AGENT_OFFLINE_MODE=1` 或断网探测
  失败）。离线档下所有非回环出站入口（云端聊天、偏好 LLM、web_fetch、语音
  云端、更新/遥测）在**发起前**被拒绝——离线模式无非回环出站尝试。
- `outbound_audit`：DNS/连接尝试的最小审计（目标类别、主机、时间、结果；
  不含正文与查询参数）——断网验证与验收报告的直接证据来源。
- `capability_report()`：聊天、偏好、嵌入、记忆、语音、网页、插件、遥测、
  更新、动作逐项声明——可用状态（available/degraded/unavailable）、当前
  后端、硬件/磁盘需求、降级说明。由真实配置与组件状态推导，不写死。
- `deterministic_actions_available()`：断网场景可用的确定性动作
  （get_current_datetime/calculate/本地只读文件动作），走 TASK-021 动作
  目录口径。

与 DOC-001（AgentArts 适配）衔接：本模块是
`AGENT_EXECUTION_PROFILE=local` 档的能力清单基础。
"""
from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any


# ---------------------------------------------------------------------------
# 离线档与出站审计
# ---------------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()
_AUDIT: list[dict[str, Any]] = []
_AUDIT_LIMIT = 200

# 出站目标类别（审计与离线裁决共用；新增入口必须登记）
OUTBOUND_CATEGORIES: frozenset[str] = frozenset({
    "llm_chat",          # 云端聊天 / LM Studio 远端（回环例外）
    "preference_llm",    # 偏好抽象 LLM
    "embedding_remote",  # 云端嵌入
    "web_fetch",         # 网页抓取
    "voice_cloud",       # 云端语音
    "telemetry",         # 遥测
    "update_check",      # 更新检查
})


def offline_mode() -> bool:
    """断网/无密钥运行档开关。

    `OS_AGENT_OFFLINE_MODE=1` 显式开启；此时全部非回环出站在发起前拒绝。
    （是否自动探测断网由调用方决定——生产中网络状态可能恢复，显式开关
    语义更可核查；`probe_connectivity()` 供启动时参考。）
    """
    return os.getenv("OS_AGENT_OFFLINE_MODE", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def loopback_allowed(url: str) -> bool:
    """回环服务（本地 LM Studio 等）不受离线档限制。"""
    from src.security.outbound import is_loopback_service
    return is_loopback_service(url)


def outbound_allowed(category: str, url: str) -> tuple[bool, str | None]:
    """统一出站裁决：离线档下仅放行回环；非离线档放行（安全防护另由
    `is_external_url_safe` 承担）。

    Returns:
        (allowed, reason)。reason 仅在拒绝时非空（进入审计与调用方错误）。
    """
    if category not in OUTBOUND_CATEGORIES:
        return False, f"未登记的出站类别：{category}"
    if not offline_mode():
        return True, None
    if loopback_allowed(url):
        return True, None
    audit_outbound(category, url, allowed=False, reason="offline_mode")
    return False, "离线模式：该类出站已停用（回环服务除外）。"


def audit_outbound(category: str, url: str, *, allowed: bool,
                   reason: str | None = None, host: str | None = None) -> None:
    """记录一次 DNS/连接尝试的最小审计（目标类别 + 主机 + 结果）。

    只存主机与类别，不存 URL 查询参数与正文。进程内有界（200 条环形）。
    """
    if not host:
        try:
            from urllib.parse import urlsplit
            host = urlsplit(url).hostname or ""
        except (ValueError, AttributeError):
            host = ""
    entry = {
        "category": category, "host": (host or "")[:120],
        "allowed": bool(allowed), "reason": reason,
        "at": round(time.time(), 3),
    }
    with _AUDIT_LOCK:
        _AUDIT.append(entry)
        if len(_AUDIT) > _AUDIT_LIMIT:
            del _AUDIT[: len(_AUDIT) - _AUDIT_LIMIT]


def outbound_audit() -> list[dict[str, Any]]:
    """出站尝试审计快照（验证用：离线模式下应只有 allowed=False 的记录）。"""
    with _AUDIT_LOCK:
        return [dict(e) for e in _AUDIT]


def reset_outbound_audit() -> None:
    """清空审计（测试用）。"""
    with _AUDIT_LOCK:
        _AUDIT.clear()


def probe_connectivity(host: str = "dns.alidns.com", timeout: float = 2.0) -> bool:
    """启动时连通性参考探测（TCP 443 握手成功即视为有网）。

    只作为提示信息，不作为离线档的强制依据（显式开关语义优先）。
    """
    try:
        addr = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addr:
            return False
        with socket.create_connection(addr[0][4], timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 能力清单：逐项可用性、后端与降级说明（由真实配置推导，不写死）
# ---------------------------------------------------------------------------

def _explicit_local_endpoint(base_url: str) -> bool:
    """回环端点是否为用户显式配置（env 覆盖）。

    configs/default.yaml 的开发默认端点（127.0.0.1:1234）不算——断网验收
    的干净机不会带着本地模型服务；只有显式 env 覆盖才视为本地模型档。
    """
    return bool(os.getenv("LM_STUDIO_BASE_URL") or os.getenv("OPENAI_BASE_URL"))


def _llm_ready() -> tuple[bool, str]:
    """云端聊天是否就绪（真实密钥 + 端点；显式配置的回环端点且密钥为占位
    时仍视为可用——本地 LM Studio 本身不需要密钥）。"""
    from src.common.config import get_llm_config
    cfg = get_llm_config()
    base_url = str(cfg.get("base_url") or "")
    api_key = str(cfg.get("api_key") or "").strip()
    placeholders = {"", "sk-your-real-key-here", "lm-studio", "sk-xxx",
                    "changeme", "your-api-key"}
    has_key = api_key.lower() not in placeholders
    local = (loopback_allowed(base_url) and _explicit_local_endpoint(base_url)) if base_url else False
    if not base_url:
        return False, "未配置端点"
    if offline_mode() and not local:
        return False, "离线模式"
    if not has_key and not local:
        return False, "缺少 API 密钥"
    return True, ("本地模型（回环）" if local else "云端模型")


def _embedding_status() -> tuple[str, str, dict[str, Any]]:
    """嵌入能力：local 后端模型可用性（轻量探测，不加载权重）。"""
    backend = (os.getenv("OS_AGENT_EMBEDDING_BACKEND")
               or os.getenv("EMBEDDING_BACKEND") or "local")
    requirement = {"model": "BAAI/bge-m3（约 2.3 GB 磁盘）", "memory": "≥ 8 GB 内存", "disk": "≥ 3 GB"}
    if backend != "local":
        return "available", f"后端 {backend}", requirement
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    if not installed:
        return ("degraded", "sentence-transformers 未安装 → 确定性哈希向量"
                            "（仅离线联调，不可用于正式召回指标）"), requirement[0], requirement
    return "available", "BGE-M3 本地（首个请求时加载）", requirement


def _voice_status() -> tuple[str, str]:
    """语音：离线 ASR/TTS 属 TASK-028；当前仅有可用性占位。"""
    if offline_mode():
        return "unavailable", "离线语音（TASK-028 未交付）；断网档下云端语音停用"
    return "unavailable", "语音操控（TASK-028 未交付）"


def capability_report() -> dict[str, Any]:
    """全量能力清单：由配置与组件状态推导。

    结构（每项）：{"status": available|degraded|unavailable,
                  "detail": 当前行为说明, "requirement": 硬件/磁盘需求（可选）,
                  "profile": basic|local_model}
    """
    llm_ok, llm_detail = _llm_ready()
    embed_status, embed_detail, embed_req = _embedding_status()
    memory_backend = (os.getenv("OS_AGENT_MEMORY_BACKEND") or "auto")

    report = {
        "profile": "local_model" if embed_status == "available" else "basic",
        "offline_mode": offline_mode(),
        "capabilities": {
            "chat": {
                "status": "available" if llm_ok else "unavailable",
                "detail": (llm_detail + "；确定性路由（遗忘/澄清）不受影响" if not llm_ok
                           else llm_detail),
            },
            "preference": {
                # 偏好：规则提取始终可用；LLM 抽象是增强（TASK-006 口径）
                "status": "degraded" if not llm_ok else "available",
                "detail": ("规则提取可用；LLM 偏好抽象不可用（缺密钥或离线）" if not llm_ok
                           else "规则 + LLM 抽象均可用"),
            },
            "embedding": {
                "status": embed_status,
                "detail": embed_detail,
                "requirement": embed_req,
            },
            "memory": {
                "status": "available",
                "detail": f"记忆后端 {memory_backend}"
                          + ("（进程内开发后端，重启即空；持久化需 milvus）" if memory_backend == "memory" else ""),
            },
            "voice": dict(zip(("status", "detail"), _voice_status())),
            "web": {
                "status": "unavailable" if offline_mode() else "available",
                "detail": "离线模式停用（仅回环放行）" if offline_mode()
                          else "web_fetch 可用（受 SSRF 防护约束）",
            },
            "plugins": {
                "status": "unavailable",
                "detail": "插件宿主（TASK-023/024 未交付）；内置基础工具不受影响",
            },
            "telemetry": {
                "status": "unavailable",
                "detail": "本产品当前无遥测上传；该条目为断网清单完整性占位",
            },
            "update_check": {
                "status": "unavailable" if offline_mode() else "degraded",
                "detail": "离线模式停用" if offline_mode()
                          else "当前无自动更新通道；手动更新仓库",
            },
            "actions": {
                "status": "available",
                "detail": "确定性动作（get_current_datetime / calculate / 本地只读文件）"
                          "断网可用；变更类动作走一次性确认（TASK-020）",
            },
        },
    }
    return report


def deterministic_actions_available() -> list[str]:
    """断网场景保证可用的确定性动作（与 TASK-021 动作目录名对齐）。"""
    return ["get_current_datetime", "calculate", "read_file", "list_directory",
            "search_files", "search_content"]
