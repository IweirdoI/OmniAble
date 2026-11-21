"""请求级执行生命周期：RunContext、有界 SSE 事件通道与流式并发槽位。

设计约束（docs/AGENT_WORKFLOW.md §3/§6）：
- RunContext 持有服务端生成的 run_id、route、单调时钟 deadline、取消事件、
  执行计数与写入状态锁；不进入长期记忆，不含第二份会话历史。
- 事件队列有界：生产者用带超时的 put 并检查取消；队满长期不消被判定为
  消费者失联，取消执行以回收资源。终态事件尽力送达，不可达时如实记录。
- 并发限制是单进程约束（线程信号量），不是分布式全局限流。
- Python 无法强制终止同步线程：worker 只在检查点响应取消，未退出的调用
  继续占用并发额度，直到真实退出才释放槽位。
"""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
import uuid
from typing import Any, Callable

from src.agent.workflow import (
    STATUS_CANCELLED,
    STATUS_TIMED_OUT,
    WorkflowBudget,
    get_workflow_budget,
)

# SSE 心跳使用注释行，不进入模型内容；旧客户端按无 data: 前缀自然忽略。
HEARTBEAT_COMMENT = ": mokoa-keepalive\n\n"
# 队列消费超时片：让执行器线程不会无期限驻留在 queue.get 上
_GET_POLL_SECONDS = 0.5
# 终端事件在已取消后的最长投递等待（消费者通常已断开，快速放弃）
_TERMINAL_GRACE_AFTER_CANCEL = 2.0


def format_sse(event: dict[str, Any]) -> str:
    """把事件序列化为一条 SSE 应用数据帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class RunAborted(RuntimeError):
    """执行在检查点被终止；status 为 cancelled 或 timed_out。"""

    def __init__(self, status: str, reason: str):
        super().__init__(f"{status}: {reason}")
        self.status = status
        self.reason = reason


class RunTimedOut(RunAborted):
    """请求总 deadline 耗尽（同步入口映射为 HTTP 504）。"""

    def __init__(self, reason: str = "deadline_exceeded"):
        super().__init__(STATUS_TIMED_OUT, reason)


class RunContext:
    """单次请求的执行上下文：预算计数、取消与写入闸门。"""

    def __init__(self, route: str = "chat", budget: WorkflowBudget | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.run_id = uuid.uuid4().hex[:12]
        self.route = route
        self.budget = budget or get_workflow_budget()
        self._clock = clock
        self.deadline = clock() + self.budget.deadline_seconds
        self._cancelled = threading.Event()
        self.cancel_reason: str | None = None
        self.abort_status = STATUS_CANCELLED
        # 写入与取消共用一把请求锁，确定“禁止新写入 / 写入已开始”的先后
        self._write_lock = threading.Lock()
        self._write_started = False
        self.write_result: dict[str, Any] | None = None
        # 执行计数（来自实际执行，不采信模型宣称）
        self.tool_calls = 0
        self.current_rounds = 0
        self.consecutive_tool_failures = 0
        self.partial = False
        self.stop_reason: str | None = None
        self.executions: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.mutations: set[str] = set()
        self._seq = itertools.count(1)

    # -- 时间与取消 ---------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def remaining_seconds(self) -> float:
        return self.deadline - self._clock()

    def check_cancel(self, where: str = "") -> None:
        if self.cancelled:
            if self.abort_status == STATUS_TIMED_OUT:
                raise RunTimedOut(self.cancel_reason or "deadline_exceeded")
            raise RunAborted(STATUS_CANCELLED, self.cancel_reason or where or "cancelled")

    def check_deadline(self, where: str = "") -> None:
        if self.remaining_seconds() <= 0:
            raise RunTimedOut(where or "deadline_exceeded")

    def check(self, where: str = "") -> None:
        """节点入口 / chunk 边界 / 工具前后 / 保存前的统一检查点。"""
        self.check_cancel(where)
        self.check_deadline(where)

    def cancel(self, reason: str = "client_disconnected", status: str = STATUS_CANCELLED) -> None:
        """请求取消（断连、停止或消费者失联）。已开始的写入不回滚。"""
        with self._write_lock:
            if not self._cancelled.is_set():
                self.cancel_reason = reason
                self.abort_status = status
            self._cancelled.set()

    def clamp_timeout(self, own_timeout: float) -> float:
        """HTTP / 抓取 / 子进程 / 模型调用超时取自身上限与剩余时间的较小值。"""
        return self.budget.remaining_timeout(self.remaining_seconds(), own_timeout)

    # -- 写入闸门 -----------------------------------------------------------
    def begin_write(self) -> bool:
        """尝试开始记忆写入；取消先发生则拒绝新写入。"""
        with self._write_lock:
            if self._cancelled.is_set():
                return False
            self.check_deadline("before_write")
            self._write_started = True
            return True

    @property
    def write_started(self) -> bool:
        with self._write_lock:
            return self._write_started

    def mark_partial(self, reason: str) -> None:
        self.partial = True
        if not self.stop_reason:
            self.stop_reason = reason

    def next_seq(self) -> int:
        """线程安全的单调递增事件序号（CPython 下 count.__next__ 原子）。"""
        return next(self._seq)


class EventChannel:
    """有界 SSE 事件通道：run_id/seq 盖章、超时 put、唯一终态。"""

    def __init__(self, ctx: RunContext):
        self._ctx = ctx
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=ctx.budget.stream_queue_capacity)
        self._terminal_lock = threading.Lock()
        self._terminal_sent = False
        self._terminal_event: dict[str, Any] | None = None

    @property
    def terminal_event(self) -> dict[str, Any] | None:
        """终态未送达时保留在内存中的副本（观测/测试用）。"""
        return self._terminal_event

    @property
    def terminal_sent(self) -> bool:
        return self._terminal_sent

    def empty(self) -> bool:
        return self._queue.empty()

    def put(self, event: dict[str, Any]) -> None:
        """生产者入队：队列满时在耐心窗口内等待，窗口耗尽判定消费者失联。

        取消后再产生的中间事件直接终止执行（消费方已离开）；任何事件都
        不会被静默丢弃——要么送达，要么以取消/失联状态如实收尾。
        """
        self._ctx.check("event")
        if self._terminal_sent:
            return
        payload = {**event, "run_id": self._ctx.run_id, "seq": self._ctx.next_seq()}
        patience_deadline = time.monotonic() + self._ctx.budget.queue_put_patience_seconds
        while True:
            self._ctx.check("event_queue")
            left = patience_deadline - time.monotonic()
            if left <= 0:
                self._ctx.cancel("event_queue_stalled")
                raise RunAborted(STATUS_CANCELLED, "event_queue_stalled")
            try:
                self._queue.put(payload, timeout=min(_GET_POLL_SECONDS, left))
                return
            except queue.Full:
                if self._ctx.cancelled:
                    raise RunAborted(
                        STATUS_CANCELLED, self._ctx.cancel_reason or "event_queue_stalled") from None

    def emit_terminal(self, event: dict[str, Any]) -> bool:
        """Claim the single terminal under lock, then enqueue outside the lock.

        A slow consumer must never hold the terminal lock and block the ASGI
        event loop's deadline path. An undelivered terminal remains available
        after the bounded token queue drains.
        """
        with self._terminal_lock:
            if self._terminal_sent:
                return False
            payload = {**event, "run_id": self._ctx.run_id, "seq": self._ctx.next_seq()}
            self._terminal_sent = True
            self._terminal_event = payload
        grace = (_TERMINAL_GRACE_AFTER_CANCEL if self._ctx.cancelled
                 else self._ctx.budget.queue_put_patience_seconds)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                self._queue.put(payload, timeout=min(.05, max(.001, deadline-time.monotonic())))
                self._terminal_event = None
                return True
            except queue.Full:
                if self._ctx.cancelled:
                    break
        return False

    def get(self, timeout: float = _GET_POLL_SECONDS) -> dict[str, Any]:
        """消费者出队；超时抛 queue.Empty（调用方据此发心跳/查终态）。"""
        return self._queue.get(timeout=timeout)

    def get_nowait(self) -> dict[str, Any]:
        return self._queue.get_nowait()

    def finish_now(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Consumer-side deadline terminal; never wait on a full queue."""
        with self._terminal_lock:
            if self._terminal_sent:
                return None
            self._terminal_sent = True
            payload = {**event, "run_id": self._ctx.run_id, "seq": self._ctx.next_seq()}
            self._terminal_event = payload
            return payload


class StreamSlots:
    """进程内并发流式执行上限；槽位由执行 worker 在真实退出时释放。"""

    def __init__(self, limit: int):
        self._limit = limit
        self._semaphore = threading.BoundedSemaphore(limit)

    @property
    def limit(self) -> int:
        return self._limit

    def try_acquire(self) -> bool:
        """非阻塞获取；失败即应返回 429，不创建无界等候。"""
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        try:
            self._semaphore.release()
        except ValueError:
            # 防御性兜底：不应发生（不会 release 超过 acquire 次数）
            pass


_stream_slots = StreamSlots(get_workflow_budget().max_active_streams)


def get_stream_slots() -> StreamSlots:
    """当前进程的流式并发槽位（server 在请求时调用，便于测试替换）。"""
    return _stream_slots
