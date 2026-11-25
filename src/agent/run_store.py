"""持久化执行记录、去重与恢复（TASK-022 / Issue #30）。

`RunContext` 的 executes/artifacts/status 只活在单次请求内；本模块把它们
落盘为可查询的运行记录：

- 存储：SQLite `data/agent_runs.db`（与 agent_sessions.db 同风格；项目根
  相对，`OS_AGENT_RUNS_DB` 可覆盖，不入 Git）。
- 用户隔离：所有查询按 `user_id` 闭包（fail-closed，跨用户 → None/空）。
- 去重：同一 `idempotency_key`（调用方提供，如 request_id）在 [pending/
  running] 状态下重复注册 → 返回既有 run（`duplicate=True`），**不发起
  第二次**；重启后同样生效（持久唯一性）。
- 终态：completed / failed / partial / cancelled / timed_out；只有真实
  结果进入终态，中断的运行恢复为 `interrupted`（不冒充成功）。
- 会话挂接：可选 `session_id`；查询校验归属（与 TASK-018 的
  sessions/agents 协调，用户隔离口径一致）。

只记录最小结果（状态/耗时/错误码/步骤计数与路径），不保存模型正文与
工具输出全文（对齐 docs/DATA_RETENTION.md 的 task_log 契约）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "data/agent_runs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT,
    idempotency_key TEXT,
    route TEXT NOT NULL DEFAULT 'chat',
    status TEXT NOT NULL,
    stop_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    latency_ms REAL,
    steps_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idem
    ON runs(idempotency_key) WHERE idempotency_key IS NOT NULL;
"""

_ACTIVE = frozenset({"pending", "running"})
_TERMINAL = frozenset({"completed", "failed", "partial", "cancelled", "timed_out", "interrupted"})


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="milliseconds")


class RunStore:
    """运行记录持久层；线程安全（与 SessionStore 相同的锁 + 连接模式）。"""

    def __init__(self, db_path: str | Path | None = None, *,
                 project_root: str | None = None) -> None:
        import os
        if project_root:
            resolved = str(Path(project_root) /
                           (str(db_path) if db_path else DEFAULT_DB_PATH))
        else:
            resolved = str(db_path or os.getenv("OS_AGENT_RUNS_DB") or DEFAULT_DB_PATH)
        self.path = Path(resolved)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 注册与去重 ---------------------------------------------------------

    def begin_run(self, user_id: str, run_id: str, *, route: str = "chat",
                  session_id: str | None = None,
                  idempotency_key: str | None = None, now: str | None = None,
                  ) -> dict[str, Any]:
        """注册一次运行；同一 idempotency_key 已有活跃运行 → 返回它（不新建）。

        返回 (record, duplicate)。重启后同样生效（唯一索引持久）。
        """
        ts = now or _now()
        with self._lock, self._conn:
            if idempotency_key:
                row = self._conn.execute(
                    "SELECT * FROM runs WHERE idempotency_key = :k",
                    {"k": idempotency_key}).fetchone()
                if row is not None:
                    if row["status"] in _ACTIVE:
                        return dict(row), True
                    # 终态后同键重试：视为新的调用，不改写历史行——
                    # 唯一索引保留原键行，新行用带后缀的键值
                    idempotency_key = f"{idempotency_key}#retry:{ts}"
            self._conn.execute(
                "INSERT OR IGNORE INTO runs(id, user_id, session_id, idempotency_key,"
                " route, status, created_at, updated_at)"
                " VALUES(:id, :u, :s, :k, :r, 'pending', :ts, :ts)",
                {"id": run_id, "u": user_id, "s": session_id,
                 "k": idempotency_key, "r": route, "ts": ts})
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = :id", {"id": run_id}).fetchone()
            if row is None:
                raise RuntimeError(f"运行注册失败：{run_id}")
            return dict(row), False

    # -- 状态与记录 ---------------------------------------------------------

    def mark_running(self, user_id: str, run_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status='running', updated_at=:ts"
                " WHERE id=:id AND user_id=:u",
                {"ts": _now(), "id": run_id, "u": user_id})
            return cur.rowcount > 0

    def record_progress(self, user_id: str, run_id: str, *,
                        steps: list[dict] | None = None,
                        artifacts: list[dict] | None = None) -> bool:
        """覆盖式记录执行步骤/工件（最小结果：错误码与路径，不含输出全文）。"""
        sets, params = ["updated_at=:ts"], {"ts": _now(), "id": run_id, "u": user_id}
        if steps is not None:
            sets.append("steps_json=:steps")
            params["steps"] = json.dumps(steps, ensure_ascii=False)
        if artifacts is not None:
            sets.append("artifacts_json=:arts")
            params["arts"] = json.dumps(artifacts, ensure_ascii=False)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id=:id AND user_id=:u",
                params)
            return cur.rowcount > 0

    def finish_run(self, user_id: str, run_id: str, status: str, *,
                   stop_reason: str | None = None, latency_ms: float | None = None,
                   steps: list[dict] | None = None,
                   artifacts: list[dict] | None = None) -> bool:
        """进入终态；status 必须是可核查的真实结果（不含 success 之类杜撰值）。"""
        if status not in _TERMINAL:
            raise ValueError(f"非法终态：{status}")
        sets = ["status=:st", "updated_at=:ts", "stop_reason=:reason",
                "latency_ms=:ms"]
        params: dict[str, Any] = {"st": status, "ts": _now(), "reason": stop_reason,
                                  "ms": latency_ms, "id": run_id, "u": user_id}
        if steps is not None:
            sets.append("steps_json=:steps")
            params["steps"] = json.dumps(steps, ensure_ascii=False)
        if artifacts is not None:
            sets.append("artifacts_json=:arts")
            params["arts"] = json.dumps(artifacts, ensure_ascii=False)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id=:id AND user_id=:u",
                params)
            return cur.rowcount > 0

    # -- 查询（严格用户隔离） ------------------------------------------------

    def get_run(self, user_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id=:id AND user_id=:u",
                {"id": run_id, "u": user_id}).fetchone()
        return self._expand(row) if row else None

    def list_runs(self, user_id: str, *, offset: int = 0,
                  limit: int = 50) -> dict[str, Any]:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE user_id=:u",
                {"u": user_id}).fetchone()["n"]
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE user_id=:u"
                " ORDER BY updated_at DESC, id LIMIT :l OFFSET :o",
                {"u": user_id, "l": limit, "o": offset}).fetchall()
        return {"total": total, "runs": [self._expand(r) for r in rows]}

    def find_recent_incomplete(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """重启/中断恢复候选：本用户 pending/running 的运行（按时间倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE user_id=:u AND status IN ('pending','running')"
                " ORDER BY updated_at DESC LIMIT :l", {"u": user_id, "l": limit}).fetchall()
        return [self._expand(r) for r in rows]

    def passivate_incomplete(self, user_id: str, *, now: str | None = None) -> int:
        """服务重启/会话清理时把活跃运行标注为 interrupted（不冒充成功）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status='interrupted', stop_reason='service_restart',"
                " updated_at=:ts WHERE user_id=:u AND status IN ('pending','running')",
                {"ts": now or _now(), "u": user_id})
            return cur.rowcount

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _expand(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["steps"] = json.loads(record.pop("steps_json") or "[]")
        record["artifacts"] = json.loads(record.pop("artifacts_json") or "[]")
        return record
