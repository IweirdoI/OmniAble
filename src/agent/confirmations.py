"""文件变更的一次性用户确认（TASK-020 / Issue #25）。

只对**受控文件变更**（write_file / edit_file / delete_file）挂闸门；只读
计算、检索等动作不经确认。一次决定绑定：

- 本次 run（run_id）与 tool call（tool_call_id）
- 规范相对路径（path）
- 目标文件状态：``target_exists`` + ``expected_revision``（目标不存在时为
  False/None，同样进入绑定，批准后目标出现即作废）
- 可信宿主凭据：``decision_token``（随 SSE confirmation_request 事件只发给
  已建立流式连接的宿主；决定端点必须回传该 token，第三方拿到 confirm_id
  也无法代投）

批准后执行时把批准的版本作为写入的 expected revision（CAS），关闭
check-then-write 窗口；取消发生在等待中或批准与写入之间都不写入。

闸门由 ``AGENT_REQUIRE_FILE_CONFIRM=1`` 显式开启（与工作区/工具的 opt-in
风格一致）；关闭时行为与 TASK-017 现状完全一致。
"""
from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass
from typing import Any

# 受控文件变更：唯一需要用户确认的动作集合（Issue #25 / Clerk 复核）
CONFIRMABLE_ACTIONS = frozenset({"write_file", "edit_file", "delete_file"})

# 结构化错误码（进入 tool_result 的 error_code，供测试与前端区分）
DENIED = "confirmation_denied"
TIMEOUT = "confirmation_timeout"
DISCONNECTED = "confirmation_disconnected"
REVISION_CHANGED = "confirmation_revision_changed"
UNKNOWN = "confirmation_unknown"
BAD_TOKEN = "confirmation_bad_token"

_DECISIONS = frozenset({"approve", "deny"})

# 确认等待上限（秒）；实际取其与请求剩余时间的较小值
MAX_WAIT_SECONDS = 120.0


@dataclass(frozen=True)
class ConfirmationRequest:
    """一次待确认的受控文件变更；字段进入 SSE confirmation_request 事件。"""

    confirm_id: str
    run_id: str
    tool_call_id: str
    action: str
    path: str                     # 规范相对路径（workspace 解析后的 POSIX 相对路径）
    target_exists: bool           # 确认时的目标存在状态（绑定"目标不存在"语义）
    expected_revision: str | None  # 确认时的文件版本；target_exists=False 时为 None
    summary: str                  # 受限变更摘要（脱敏后截断）
    decision_token: str = ""      # 可信宿主凭据：仅 SSE 宿主可见，决定端点必须回传


@dataclass
class _Entry:
    request: ConfirmationRequest
    decided: threading.Event
    decision: str | None = None


class ConfirmationRegistry:
    """run 内一次性决定的注册与投递；线程安全。

    - register：同 confirm_id 幂等保留首个（决定不丢）
    - decide：校验 decision_token；首个决定生效，重复提交返回首个
    - wait：轮询返回，便于外层插入取消检查
    - drop_run：run 终态清理，晚到决定失效
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def register(self, request: ConfirmationRequest) -> ConfirmationRequest:
        with self._lock:
            if request.confirm_id not in self._entries:
                self._entries[request.confirm_id] = _Entry(
                    request=request, decided=threading.Event())
        return request

    def decide(self, confirm_id: str, decision: str,
               token: str) -> tuple[dict[str, Any] | None, str | None]:
        """提交一次性决定。

        Returns:
            (payload, error)。error 为 None 表示成功；``unknown`` 表示注册表
            无此 id（API 层 404）；``bad_token`` 表示宿主凭据不匹配（API 层 403）。
        """
        if decision not in _DECISIONS:
            raise ValueError("decision 必须是 approve 或 deny")
        with self._lock:
            entry = self._entries.get(confirm_id)
            if entry is None:
                return None, "unknown"
            if not secrets.compare_digest(entry.request.decision_token, token or ""):
                return None, BAD_TOKEN
            duplicate = entry.decision is not None
            if not duplicate:
                entry.decision = decision
            entry.decided.set()
            return {"confirm_id": confirm_id, "decision": entry.decision,
                    "duplicate": duplicate}, None

    def wait(self, confirm_id: str, timeout: float,
             poll: float = 0.2) -> tuple[str | None, bool]:
        """等待决定；(decision, decided)。decided=True 而 decision=None 表示
        请求已被 drop（运行结束/连接断开）。"""
        with self._lock:
            entry = self._entries.get(confirm_id)
        if entry is None:
            return None, False
        waited = 0.0
        while waited < timeout:
            if entry.decided.wait(min(poll, max(0.0, timeout - waited))):
                return entry.decision, True
            waited += poll
        return None, entry.decided.is_set()

    def drop_run(self, run_id: str) -> int:
        """run 终态时清理其全部确认请求，返回清理数量。"""
        with self._lock:
            stale = [cid for cid, e in self._entries.items()
                     if e.request.run_id == run_id]
            for cid in stale:
                entry = self._entries.pop(cid)
                entry.decided.set()  # 唤醒可能的等待者，由其按 DISCONNECTED 处理
            return len(stale)

    def get(self, confirm_id: str) -> ConfirmationRequest | None:
        with self._lock:
            entry = self._entries.get(confirm_id)
            return entry.request if entry else None


_REGISTRY = ConfirmationRegistry()


def required() -> bool:
    """确认闸门开关：AGENT_REQUIRE_FILE_CONFIRM=1 时受控文件变更需逐次确认。"""
    return os.getenv("AGENT_REQUIRE_FILE_CONFIRM", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def registry() -> ConfirmationRegistry:
    """进程级注册表（服务端单进程；测试可自行构造实例做纯单元验证）。"""
    return _REGISTRY


def new_confirm_id() -> str:
    return "cfm-" + secrets.token_hex(8)


def new_decision_token() -> str:
    return secrets.token_hex(16)
