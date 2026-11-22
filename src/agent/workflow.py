"""Agent 工作流契约：执行预算与 workflow 响应对象（设计见 docs/AGENT_WORKFLOW.md §3/§5）。

预算是资源保护初值，不是性能验收成绩；数值统一在 configs/default.yaml 的
``agent.workflow`` 段声明，非法数值在加载时明确报错（启动即失败，而不是带病运行）。
客户端请求不能提高额度——ChatRequest 不携带任何预算字段。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from src.common import config as app_config

# ---------------------------------------------------------------------------
# 终态取值（同步响应与 SSE done/error 复用同一套状态）
# ---------------------------------------------------------------------------
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMED_OUT = "timed_out"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"

_TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED,
    STATUS_CANCELLED, STATUS_TIMED_OUT, STATUS_NEEDS_CLARIFICATION,
})

# workflow 对象中列表与文本的输出上限：不向前端发送无界负载
_MAX_STEPS = 16
_MAX_ARTIFACTS = 8
_MAX_TEXT_CHARS = 500


@dataclass(frozen=True)
class WorkflowBudget:
    """请求级执行预算（服务端配置，客户端不可提升）。"""

    max_tool_rounds: int = 2            # 工具请求轮次（第一版上限 4）
    max_calls_per_round: int = 4        # 每轮最多工具调用数
    max_total_tool_calls: int = 8       # 整个请求累计工具调用数
    max_consecutive_tool_failures: int = 2  # 连续工具失败次数上限
    deadline_seconds: float = 180.0     # 请求总 deadline（单调时钟）
    stream_queue_capacity: int = 64     # 流式事件队列容量
    max_active_streams: int = 4         # 每进程并发流式执行上限（非分布式限流）
    heartbeat_seconds: float = 15.0     # SSE 心跳间隔（注释行，不进模型内容）
    queue_put_patience_seconds: float = 30.0   # 队列满时生产者等待消费者的耐心窗口

    def remaining_timeout(self, remaining_seconds: float, own_timeout: float) -> float:
        """HTTP / 抓取 / 子进程等调用的超时 = 自身上限与请求剩余时间的较小值。"""
        return max(0.05, min(own_timeout, remaining_seconds))


def _require_int(section: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"agent.workflow.{key} 必须是整数")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"agent.workflow.{key} 必须是整数，当前为 {raw!r}") from exc
    if not lo <= value <= hi:
        raise ValueError(f"agent.workflow.{key}={value} 超出允许范围 [{lo}, {hi}]")
    return value


def _require_float(section: dict, key: str, default: float, lo: float, hi: float) -> float:
    raw = section.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"agent.workflow.{key} 必须是数值")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"agent.workflow.{key} 必须是数值，当前为 {raw!r}") from exc
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"agent.workflow.{key}={value} 超出允许范围 [{lo}, {hi}]")
    return value


@lru_cache(maxsize=1)
def load_workflow_budget() -> WorkflowBudget:
    """从 configs/default.yaml 读取并校验预算；非法数值抛 ValueError（启动期显式失败）。"""
    agent_section = app_config.get_section("agent") or {}
    section = agent_section.get("workflow") or {}
    if not isinstance(section, dict):
        raise ValueError("agent.workflow 必须是配置段（映射），当前为 " + type(section).__name__)
    return WorkflowBudget(
        max_tool_rounds=_require_int(section, "max_tool_rounds", 2, 1, 4),
        max_calls_per_round=_require_int(section, "max_calls_per_round", 4, 1, 8),
        max_total_tool_calls=_require_int(section, "max_total_tool_calls", 8, 1, 32),
        max_consecutive_tool_failures=_require_int(
            section, "max_consecutive_tool_failures", 2, 1, 8),
        deadline_seconds=_require_float(section, "deadline_seconds", 180.0, 1.0, 3600.0),
        stream_queue_capacity=_require_int(section, "stream_queue_capacity", 64, 8, 1024),
        max_active_streams=_require_int(section, "max_active_streams", 4, 1, 64),
        heartbeat_seconds=_require_float(section, "heartbeat_seconds", 15.0, 1.0, 120.0),
        queue_put_patience_seconds=_require_float(
            section, "queue_put_patience_seconds", 30.0, 1.0, 300.0),
    )


def get_workflow_budget() -> WorkflowBudget:
    return load_workflow_budget()


def route_request(message: str) -> str:
    """Only unambiguous, imperative forgetting bypasses generation.

    Quoted/negated/discussed deletion is data; compound or vague imperatives
    require clarification. Ordinary chat keeps the existing recall path.
    """
    text = message.strip()
    command = re.match(r"^(?:请)?(?:帮我)?(?:忘掉|忘记|删除|清除|删掉)\s*(.*)", text)
    # Filesystem deletion is an Agent task, never a memory-forgetting command.
    if command and re.search(r"文件|目录|[/\\]|\b[\w-]+\.[a-zA-Z0-9]{1,12}\b", command[1]):
        return "tool_task"
    if command:
        target = command[1].strip(" 。！!？?")
        if (not target or target in {"那个", "这个", "它", "一些", "一些内容", "之前那条"}
                or re.search(r"[，,；;。]|然后|另外|再帮|再问|并且|以及|是什么意思|这个词|怎么|如何|吗|[？?]", target)
                or any(c in target for c in '“”「」『』\"')):
            return "clarification"
        return "forget"
    if re.search(r"读取|读一下|整理.*(?:文件|资料)|生成.*(?:文件|笔记|报告)|写入.*文件|编辑.*文件", text):
        return "tool_task"
    return "chat"


def _clip_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= _MAX_TEXT_CHARS else text[:_MAX_TEXT_CHARS] + "…"


def build_workflow_payload(
    *,
    run_id: str,
    route: str,
    status: str,
    stop_reason: str | None = None,
    steps: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    budget_used: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 version=1 的 workflow 响应对象；同步响应与 SSE 终态共用。

    列表与文本做数量/长度裁剪，不包含 embedding、完整工具原文或个人环境路径。
    """
    if status not in _TERMINAL_STATUSES:
        raise ValueError(f"未知的 workflow 状态：{status!r}")
    safe_steps = [
        {k: (_clip_text(v) if isinstance(v, str) else v) for k, v in step.items()}
        for step in (steps or [])[:_MAX_STEPS]
    ]
    safe_artifacts = [
        {k: (_clip_text(v) if isinstance(v, str) else v) for k, v in item.items()}
        for item in (artifacts or [])[:_MAX_ARTIFACTS]
    ]
    return {
        "version": 1,
        "run_id": run_id,
        "route": route,
        "status": status,
        "stop_reason": _clip_text(stop_reason) if stop_reason else None,
        "steps": safe_steps,
        "artifacts": safe_artifacts,
        "budget_used": dict(budget_used or {}),
    }


def budget_usage(
    *, tool_rounds: int, tool_calls: int, deadline_seconds: float,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    """budget_used 摘要：计数来自实际执行，不采信模型宣称。"""
    usage: dict[str, Any] = {
        "tool_rounds": int(tool_rounds),
        "tool_calls": int(tool_calls),
        "deadline_seconds": float(deadline_seconds),
    }
    if elapsed_ms is not None:
        usage["elapsed_ms"] = round(float(elapsed_ms), 3)
    return usage
