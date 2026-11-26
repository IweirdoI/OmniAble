"""持久化执行记录、去重与恢复（TASK-022 / Issue #30）。

`RunContext` 的 executes/artifacts/status 只活在单次请求内；本模块把它们
落盘为可查询的运行记录：

- 存储：SQLite `data/agent_runs.db`（与 agent_sessions.db 同风格；项目根
  相对，`OS_AGENT_RUNS_DB` 可覆盖，不入 Git）。
- 用户隔离：所有查询按 `user_id` 闭包（fail-closed，跨用户 → None/空）。
- 去重：同一用户和 `idempotency_key` 始终返回首次运行（包括终态），
  重启后同样生效。
- 终态：completed / failed / partial / cancelled / timed_out；只有真实
  结果进入终态，中断的运行恢复为 `interrupted`（不冒充成功）。
- 会话挂接：可选 `session_id`；查询校验归属（与 TASK-018 的
  sessions/agents 协调，用户隔离口径一致）。

只记录最小结果（状态/耗时/错误码/步骤计数与路径），不保存模型正文与
工具输出全文（对齐 docs/DATA_RETENTION.md 的 task_log 契约）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "data/agent_runs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    owner_pid INTEGER,
    confirmed_steps_json TEXT NOT NULL DEFAULT '[]',
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
    ON runs(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
"""

_ACTIVE = frozenset({"pending", "running"})
_TERMINAL = frozenset({"completed", "failed", "partial", "cancelled", "timed_out",
                       "interrupted", "needs_review"})
_STEP_FIELDS = ("id", "name", "ok", "error_code", "elapsed_ms", "executed")
_ARTIFACT_FIELDS = ("path", "bytes", "deleted")


def _minimal_items(items: list[dict], fields: tuple[str, ...]) -> str:
    return json.dumps([
        {field: item[field] for field in fields if field in item
         and isinstance(item[field], (str, int, float, bool, type(None)))}
        for item in items if isinstance(item, dict)
    ], ensure_ascii=False)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import WinDLL, get_last_error, wintypes

        kernel = WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return get_last_error() != 87  # Invalid PID; access denied is inconclusive.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _now() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="milliseconds")


class RunStore:
    """运行记录持久层；线程安全（与 SessionStore 相同的锁 + 连接模式）。"""

    def __init__(self, db_path: str | Path | None = None, *,
                 project_root: str | None = None) -> None:
        self.path = Path(db_path or os.getenv("OS_AGENT_RUNS_DB") or DEFAULT_DB_PATH)
        if project_root and not self.path.is_absolute():
            self.path = Path(project_root) / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """v1 → v2：补 owner_pid / confirmed_steps_json 列（旧库平滑升级）。"""
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "owner_pid" not in cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER")
        if "confirmed_steps_json" not in cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN confirmed_steps_json TEXT NOT NULL DEFAULT '[]'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 注册与去重 ---------------------------------------------------------

    def begin_run(self, user_id: str, run_id: str, *, route: str = "chat",
                  session_id: str | None = None,
                  idempotency_key: str | None = None, now: str | None = None,
                  ) -> tuple[dict[str, Any], bool]:
        """注册一次运行；**同一 (用户, 操作键) 永远返回首次运行**（标准幂等）。

        语义（TASK-022 复核项：并发及重启后稳定去重）：
        - 首次调用：创建并返回 (record, duplicate=False)
        - 同键再次调用（无论首次处于活跃还是已终态）：返回首次记录
          (duplicate=True)——调用方据此复用首次结果，绝不开启第二条运行
        - 并发：唯一索引 (user_id, idempotency_key) + 忽略后按键回查，
          任何时序下最多一条
        - 重启：唯一索引持久，跨进程同样生效
        """
        ts = now or _now()
        with self._lock, self._conn:
            if idempotency_key:
                row = self._conn.execute(
                    "SELECT * FROM runs WHERE user_id = :u AND idempotency_key = :k",
                    {"u": user_id, "k": idempotency_key}).fetchone()
                if row is not None:
                    return dict(row), True  # 同键 = 同一逻辑操作：永远返回首次
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO runs(id, user_id, owner_pid, session_id,"
                " idempotency_key, route, status, created_at, updated_at)"
                " VALUES(:id, :u, :pid, :s, :k, :r, 'pending', :ts, :ts)",
                {"id": run_id, "u": user_id, "pid": os.getpid(), "s": session_id,
                 "k": idempotency_key, "r": route, "ts": ts})
            # 并发同键竞态兜底：IGNORE 可能因唯一索引(用户,键)跳过——
            # 按键回查并返回既有记录（绝不开启第二条）
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = :id AND user_id = :u",
                {"id": run_id, "u": user_id}).fetchone()
            if row is None and idempotency_key:
                row = self._conn.execute(
                    "SELECT * FROM runs WHERE user_id = :u AND idempotency_key = :k"
                    " ORDER BY created_at LIMIT 1",
                    {"u": user_id, "k": idempotency_key}).fetchone()
                if row is not None:
                    return dict(row), True
            if row is None:
                raise RuntimeError(f"运行注册失败：{run_id}")
            return dict(row), inserted.rowcount == 0

    # -- 状态与记录 ---------------------------------------------------------

    def mark_running(self, user_id: str, run_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status='running', updated_at=:ts"
                " WHERE id=:id AND user_id=:u AND status='pending'",
                {"ts": _now(), "id": run_id, "u": user_id})
            return cur.rowcount > 0

    def record_progress(self, user_id: str, run_id: str, *,
                        steps: list[dict] | None = None,
                        artifacts: list[dict] | None = None) -> bool:
        """覆盖式记录执行步骤/工件（最小结果：错误码与路径，不含输出全文）。"""
        sets, params = ["updated_at=:ts"], {"ts": _now(), "id": run_id, "u": user_id}
        if steps is not None:
            sets.append("steps_json=:steps")
            params["steps"] = _minimal_items(steps, _STEP_FIELDS)
        if artifacts is not None:
            sets.append("artifacts_json=:arts")
            params["arts"] = _minimal_items(artifacts, _ARTIFACT_FIELDS)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id=:id AND user_id=:u",
                params)
            return cur.rowcount > 0

    def finish_run(self, user_id: str, run_id: str, status: str, *,
                   stop_reason: str | None = None, latency_ms: float | None = None,
                   steps: list[dict] | None = None,
                   artifacts: list[dict] | None = None) -> bool:
        """进入终态；status 必须是可核查的真实结果（不含 success 之类杜撰值）。

        终态禁止回写：已是终态的运行再次 finish → 返回 False（不覆盖），
        修复「晚到的结果还能改写中断状态」。
        """
        if status not in _TERMINAL:
            raise ValueError(f"非法终态：{status}")
        sets = ["status=:st", "updated_at=:ts", "stop_reason=:reason",
                "latency_ms=:ms"]
        params: dict[str, Any] = {"st": status, "ts": _now(), "reason": stop_reason,
                                  "ms": latency_ms, "id": run_id, "u": user_id}
        if steps is not None:
            sets.append("steps_json=:steps")
            params["steps"] = _minimal_items(steps, _STEP_FIELDS)
        if artifacts is not None:
            sets.append("artifacts_json=:arts")
            params["arts"] = _minimal_items(artifacts, _ARTIFACT_FIELDS)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE id=:id AND user_id=:u"
                " AND status IN " + "(" + ",".join(
                    f"'{t}'" for t in sorted(_ACTIVE)) + ")",
                params)
            return cur.rowcount > 0

    def cancel_run(self, user_id: str, run_id: str, *,
                   reason: str = "user_stop") -> dict[str, Any] | None:
        """取消入口：活跃运行 → cancelled（不启动新步骤）；已终态不可取消。

        Returns:
            {"cancelled": bool, "status": 当前状态}；运行不存在/跨用户 → None。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "UPDATE runs SET status='cancelled', stop_reason=:reason,"
                " updated_at=:ts WHERE id=:id AND user_id=:u"
                " AND status IN ('pending','running') RETURNING status",
                {"reason": reason, "ts": _now(), "id": run_id, "u": user_id}).fetchone()
            if row is not None:
                return {"cancelled": True, "status": row["status"]}
            row = self._conn.execute(
                "SELECT status FROM runs WHERE id=:id AND user_id=:u",
                {"id": run_id, "u": user_id}).fetchone()
            return {"cancelled": False, "status": row["status"]} if row is not None else None

    def mark_needs_review(self, user_id: str, run_id: str, *,
                          reason: str = "external_result_unknown") -> bool:
        """外部提交后取不到回执 → 转人工核对（不冒充成功/失败）。

        仅活跃运行可转：已终态（completed/failed 等）的结果已是事实，
        不改写；needs_review 本身也是终态（一次转入，不抖动）。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status='needs_review', stop_reason=:reason,"
                " updated_at=:ts WHERE id=:id AND user_id=:u AND status IN ("
                + ",".join(f"'{t}'" for t in sorted(_ACTIVE)) + ")",
                {"reason": reason, "ts": _now(), "id": run_id, "u": user_id})
            return cur.rowcount > 0

    def mark_step_confirmed(self, user_id: str, run_id: str, step_id: str) -> bool:
        """登记已确认步骤：恢复/重试时跳过，不重复执行。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT confirmed_steps_json FROM runs WHERE id=:id AND user_id=:u",
                {"id": run_id, "u": user_id}).fetchone()
            if row is None:
                return False
            confirmed = set(json.loads(row["confirmed_steps_json"] or "[]"))
            confirmed.add(str(step_id))
            self._conn.execute(
                "UPDATE runs SET confirmed_steps_json=:c, updated_at=:ts"
                " WHERE id=:id AND user_id=:u",
                {"c": json.dumps(sorted(confirmed), ensure_ascii=False),
                 "ts": _now(), "id": run_id, "u": user_id})
            return True

    def confirmed_steps(self, user_id: str, run_id: str) -> list[str]:
        """已确认步骤 id（恢复时跳过这些步骤）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT confirmed_steps_json FROM runs WHERE id=:id AND user_id=:u",
                {"id": run_id, "u": user_id}).fetchone()
        return json.loads(row["confirmed_steps_json"] or "[]") if row else []

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
        """按更新时间列出无存活进程负责的 pending/running。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE user_id=:u AND status IN ('pending','running')"
                " ORDER BY updated_at DESC, id", {"u": user_id}).fetchall()
        live_by_pid = {os.getpid(): True, None: False}
        orphans = []
        for row in rows:
            owner = row["owner_pid"]
            if owner not in live_by_pid:
                live_by_pid[owner] = _process_is_alive(owner)
            if not live_by_pid[owner]:
                orphans.append(self._expand(row))
                if len(orphans) >= limit:
                    break
        return orphans

    def passivate_incomplete(self, user_id: str, *, now: str | None = None) -> list[dict[str, Any]]:
        """服务重启/会话清理：只把**孤儿**运行（不属于本进程）标注为
        interrupted，并**返回本次标注的候选记录**（修「返回空候选」）；
        本进程正在执行的运行不受影响。"""
        orphans = self.find_recent_incomplete(user_id, limit=500)
        if not orphans:
            return []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "UPDATE runs SET status='interrupted', stop_reason='service_restart',"
                " updated_at=:ts WHERE user_id=:u AND status IN ('pending','running')"
                " AND id IN (" + ",".join(
                      ":id" + str(i) for i in range(len(orphans))) + ") RETURNING *",
                {"ts": now or _now(), "u": user_id,
                 **{f"id{i}": record["id"] for i, record in enumerate(orphans)}}).fetchall()
        # 返回标注后的完整记录（含 steps/artifacts），供恢复界面直接展示
        updated = {row["id"]: self._expand(row) for row in rows}
        return [updated[record["id"]] for record in orphans if record["id"] in updated]

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _expand(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["steps"] = json.loads(record.pop("steps_json") or "[]")
        record["artifacts"] = json.loads(record.pop("artifacts_json") or "[]")
        # 恢复候选携带已确认步骤：重试时跳过这些步骤（Clerk 复核要求）
        record["confirmed_steps"] = json.loads(
            record.pop("confirmed_steps_json", "[]") or "[]")
        return record


def test_find_recent_incomplete_only_orphans(store) -> None:
    """候选 = 孤儿运行（owner_pid 非本进程或 NULL）；本进程活跃不在候选。

    （复核项：活跃所有权与恢复候选分开。上组 test_progress_recording 里
    本进程 mark_running 的运行不再被视为「恢复候选」，恢复也不会误改它。）
    """
    store.begin_run("u_a", "mine-1")
    store.mark_running("u_a", "mine-1")
    assert store.find_recent_incomplete("u_a") == []  # 本进程运行不是恢复候选

    import sqlite3 as _sq
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO runs(id, user_id, owner_pid, status, created_at, updated_at)"
            " VALUES('dead-1', 'u_a', 999999, 'running', 't', 't')")
    candidates = store.find_recent_incomplete("u_a")
    assert [c["id"] for c in candidates] == ["dead-1"]


def test_finish_run_allows_terminal_transition_to_needs_review(store) -> None:
    """needs_review 白名单转变：外部结果未知后晚到 completed 不得覆盖。"""
    store.begin_run("u_a", "rn-3")
    store.mark_running("u_a", "rn-3")
    store.mark_needs_review("u_a", "rn-3")
    # finish_run 的非覆盖条件应包含 needs_review
    assert store.finish_run("u_a", "rn-3", "completed") is False
    assert store.get_run("u_a", "rn-3")["status"] == "needs_review"

    # interrupted 同样禁回写
    store.begin_run("u_a", "rn-4")
    store.mark_running("u_a", "rn-4")
    store.finish_run("u_a", "rn-4", "interrupted")
    assert store.finish_run("u_a", "rn-4", "completed") is False


# ---------------------------------------------------------------------------
# 活跃运行的进程内注册表（TASK-022 复核整改）：取消入口必须能触达正在
# 执行的 RunContext——run_store.cancel_run 落盘状态后，经此表对活跃
# 上下文发出协作取消（下一步骤启动前由 ctx.check 边界拦截）。
# ---------------------------------------------------------------------------

class ActiveRunRegistry:
    """run_id → RunContext 的进程内注册；线程安全。

    服务层在运行开始时 register、终态（finally）时 unregister。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: dict[str, Any] = {}

    def register(self, run_id: str, ctx: Any) -> None:
        with self._lock:
            self._contexts[run_id] = ctx

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._contexts.pop(run_id, None)

    def cancel(self, run_id: str, reason: str = "user_stop") -> bool:
        """向活跃上下文发出协作取消；返回是否命中正在执行的运行。"""
        with self._lock:
            ctx = self._contexts.get(run_id)
        if ctx is None:
            return False
        try:
            ctx.cancel(reason)
            return True
        except Exception:
            return False


_ACTIVE_RUNS = ActiveRunRegistry()


def active_runs() -> ActiveRunRegistry:
    """进程级活跃运行注册表。"""
    return _ACTIVE_RUNS
