"""本地定时任务与错过执行处理（TASK-031 / Issue #39）。

宿主运行期间触发的本地调度器；任务定义与触发历史持久化（SQLite
`data/scheduler.db`，与 agent_runs.db 同风格），重启后可见错过的时点。

核心设计：
- **可控时钟**：`clock` / `sleep` 可注入——测试用假时钟驱动睡眠唤醒、
  重启、夏令时（DST）重复/跳时与双实例竞争。
- **时区与预览**：daily 任务按 `zoneinfo` 解析本地时间；`preview()` 给出
  下次执行时刻。DST 语义：重复时刻（fall-back）只触发一次（按 UTC 时刻
  去重）；跳时（spring-forward）不存在的本地时刻顺延到下一有效时刻。
- **单次时点只创建一条运行**：`fire_log` 主键 (task_id, fire_key) 去重 +
  运行记录幂等键 `sched:{task_id}:{fire_key}`（对接 TASK-022），双实例
  竞争与重试下同一时点最多一条运行。
- **错过执行**：重启/睡眠后从 last_fired 到 now 之间错过的时点——
  最近一次补发执行（outcome=missed 标注），更早的仅记录 missed，
  不逐条补发风暴；界面据 fire_log 显示错过。
- **双实例互斥**：`leases` 表租约 + 心跳；持有租约的实例才触发，另一
  实例待命；持有者崩溃后租约过期由存活实例接管。
- **高影响写入无人确认不执行**：变更类动作（write/edit/delete_file）
  在调度触发时没有交互通道 → 不执行，记录 waiting_confirmation
  （「需人在场则等待」，复用 TASK-020 的受控文件变更集合）。
- **每次运行重查身份与权限**：触发时校验任务归属用户、动作在当前开关
  下可用（TASK-021 动作目录口径）；插件版本重查待 TASK-024 交付后接入。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DB_PATH = "data/scheduler.db"
DEFAULT_LEASE_TTL = 30.0
DEFAULT_TICK_INTERVAL = 1.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    schedule_kind TEXT NOT NULL,
    schedule_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_fired_at TEXT,
    next_fire_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS leases (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fire_log (
    task_id TEXT NOT NULL,
    fire_key TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    run_id TEXT,
    outcome TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY (task_id, fire_key)
);
"""

# 变更类动作：调度触发时无交互通道 → 无人确认不执行（TASK-020 口径）。
# exec_terminal 同样属变更类（可改文件/执行系统操作）——终端动作在
# 无人确认时也不得执行（TASK-031 复核项）。
_CONFIRMABLE = frozenset({
    "write_file", "edit_file", "delete_file", "exec_terminal",
})


def _now_iso(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TaskStore:
    """任务定义、租约与触发历史的持久层；线程安全。"""

    def __init__(self, db_path: str | Path | None = None, *,
                 project_root: str | None = None) -> None:
        if project_root:
            resolved = str(Path(project_root) /
                           (str(db_path) if db_path else DEFAULT_DB_PATH))
        else:
            resolved = str(db_path or os.getenv("OS_AGENT_SCHEDULER_DB") or DEFAULT_DB_PATH)
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

    # -- 任务 CRUD ----------------------------------------------------------

    def create_task(self, user_id: str, action: str, args: dict, *,
                    schedule_kind: str, schedule: dict,
                    now: str | None = None) -> dict[str, Any]:
        """新建任务；schedule_kind ∈ once | interval | daily。

        schedule：
          once     {"at": ISO 本地时间}
          interval {"every_seconds": N, "start": ISO（可选，缺省 now）}
          daily    {"time": "HH:MM", "tz": "Asia/Shanghai"}
        """
        if schedule_kind not in {"once", "interval", "daily"}:
            raise ValueError(f"不支持的调度类型：{schedule_kind}")
        task_id = "task-" + uuid.uuid4().hex[:12]
        ts = now or datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks(id, user_id, action, args_json, schedule_kind,"
                " schedule_json, enabled, created_at, updated_at)"
                " VALUES(:id, :u, :a, :args, :kind, :sched, 1, :ts, :ts)",
                {"id": task_id, "u": user_id, "a": action,
                 "args": json.dumps(args, ensure_ascii=False),
                 "kind": schedule_kind,
                 "sched": json.dumps(schedule, ensure_ascii=False), "ts": ts})
        return self.get_task(user_id, task_id)

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=:id AND user_id=:u",
                {"id": task_id, "u": user_id}).fetchone()
        return self._expand(row) if row else None

    def list_tasks(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE user_id=:u ORDER BY updated_at DESC",
                {"u": user_id}).fetchall()
        return [self._expand(r) for r in rows]

    def set_enabled(self, user_id: str, task_id: str, enabled: bool) -> bool:
        """暂停/恢复任务。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE tasks SET enabled=:e, updated_at=:ts WHERE id=:id AND user_id=:u",
                {"e": 1 if enabled else 0, "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "id": task_id, "u": user_id})
            return cur.rowcount > 0

    def delete_task(self, user_id: str, task_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM tasks WHERE id=:id AND user_id=:u",
                {"id": task_id, "u": user_id})
            return cur.rowcount > 0

    def update_fire_state(self, task_id: str, *, last_fired_at: str,
                          next_fire_at: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET last_fired_at=:lf, next_fire_at=:nf, updated_at=:ts"
                " WHERE id=:id",
                {"lf": last_fired_at, "nf": next_fire_at,
                 "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "id": task_id})

    # -- 触发历史（幂等去重） ------------------------------------------------

    def claim_fire(self, task_id: str, fire_key: str, *, fired_at: str,
                   outcome: str, run_id: str | None = None,
                   detail: str | None = None) -> bool:
        """登记一次触发；同 (task_id, fire_key) 已存在 → False（不重复执行）。

        主键去重保证「单次时点只创建一条运行」——双实例竞争与重试下
        同一时点最多一条。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO fire_log(task_id, fire_key, fired_at,"
                " run_id, outcome, detail) VALUES(:t, :k, :at, :r, :o, :d)",
                {"t": task_id, "k": fire_key, "at": fired_at, "r": run_id,
                 "o": outcome, "d": detail})
            return cur.rowcount > 0

    def update_fire_outcome(self, task_id: str, fire_key: str, *,
                            outcome: str, run_id: str | None = None,
                            detail: str | None = None) -> None:
        """触发完成后更新结果（executed/failed/waiting_confirmation 等）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE fire_log SET outcome=:o, run_id=:r, detail=:d"
                " WHERE task_id=:t AND fire_key=:k",
                {"o": outcome, "r": run_id, "d": detail,
                 "t": task_id, "k": fire_key})

    def all_enabled_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE enabled=1").fetchall()
        return [self._expand(r) for r in rows]

    def fire_history(self, user_id: str, task_id: str,
                     *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.* FROM fire_log f JOIN tasks t ON t.id = f.task_id"
                " WHERE f.task_id=:tid AND t.user_id=:u"
                " ORDER BY f.fired_at DESC LIMIT :l",
                {"tid": task_id, "u": user_id, "l": limit}).fetchall()
        return [dict(r) for r in rows]

    # -- 租约（双实例互斥） --------------------------------------------------

    def acquire_lease(self, name: str, owner: str, *, ttl: float,
                      now: float) -> bool:
        """获取/续租；持有者崩溃（expires_at 过期）后由新实例接管。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT owner, expires_at FROM leases WHERE name=:n",
                {"n": name}).fetchone()
            if row is not None and row["owner"] != owner and row["expires_at"] > now:
                return False  # 他人持有且未过期
            self._conn.execute(
                "INSERT OR REPLACE INTO leases(name, owner, expires_at)"
                " VALUES(:n, :o, :e)",
                {"n": name, "o": owner, "e": now + ttl})
            return True

    def release_lease(self, name: str, owner: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM leases WHERE name=:n AND owner=:o",
                {"n": name, "o": owner})

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _expand(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["args"] = json.loads(record.pop("args_json") or "{}")
        record["schedule"] = json.loads(record.pop("schedule_json") or "{}")
        record["enabled"] = bool(record["enabled"])
        return record


# ---------------------------------------------------------------------------
# 触发时刻计算（时区/DST 语义）
# ---------------------------------------------------------------------------

def _zone(tz_name: str | None) -> ZoneInfo:
    if not tz_name:
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{tz_name}") from exc


def next_fire_after(task: dict[str, Any], after_iso: str) -> str | None:
    """计算 after 之后的下一次触发时刻（ISO 本地时间）；无后续返回 None。

    DST 语义：
    - 重复时刻（fall-back，同一本地时间出现两次）：按 UTC 时刻推进，
      同一 UTC 时刻只产生一次触发（不双触发）；
    - 跳时（spring-forward，本地时刻不存在）：顺延到下一有效时刻。
    """
    kind = task["schedule_kind"]
    sched = task["schedule"]
    after = _parse_iso(after_iso)
    if kind == "once":
        at = _parse_iso(sched["at"])
        return at.isoformat(timespec="seconds") if at > after else None
    if kind == "interval":
        every = float(sched["every_seconds"])
        if every <= 0:
            raise ValueError("interval 必须为正秒数")
        start = _parse_iso(sched["start"]) if sched.get("start") else after
        # 按 UTC 时间戳推进：DST 切换不影响间隔计算
        base_ts = max(start.timestamp(), after.timestamp())
        steps = int((base_ts - start.timestamp()) // every) + 1
        nxt_ts = start.timestamp() + steps * every
        if nxt_ts <= after.timestamp():
            nxt_ts += every
        return datetime.fromtimestamp(nxt_ts, tz=after.tzinfo).isoformat(timespec="seconds")
    if kind == "daily":
        tz = _zone(sched.get("tz"))
        hh, mm = (int(x) for x in str(sched["time"]).split(":"))
        local_after = after.astimezone(tz)
        candidate = local_after.replace(hour=hh, minute=mm, second=0, microsecond=0, fold=0)
        if candidate <= local_after:
            candidate = (local_after + timedelta(days=1)).replace(
                hour=hh, minute=mm, second=0, microsecond=0, fold=0)
        # 跳时：不存在的本地时刻（spring-forward）→ 顺延到下一有效时刻。
        # zoneinfo 对 nonexistent time 的 fold=0 解析会映射到跳变后的
        # 有效时刻（如 02:30 → 03:30），该时刻即顺延结果。
        utc_ts = candidate.timestamp()
        resolved = datetime.fromtimestamp(utc_ts, tz=tz)
        if (resolved.hour, resolved.minute) != (hh, mm):
            # 解析结果已是跳变后的有效时刻；若仍早于 after 则再取次日
            if resolved <= local_after:
                resolved = (local_after + timedelta(days=1)).replace(
                    hour=hh, minute=mm, second=0, microsecond=0, fold=0)
                resolved = datetime.fromtimestamp(resolved.timestamp(), tz=tz)
        return resolved.isoformat(timespec="seconds")
    return None


def missed_fires(task: dict[str, Any], *, since_iso: str,
                 now_iso: str, cap: int = 100) -> list[str]:
    """列出 (since, now] 之间错过的触发时刻（升序，最多 cap 条）。"""
    fires: list[str] = []
    cursor = since_iso
    for _ in range(cap):
        nxt = next_fire_after(task, cursor)
        if nxt is None or _parse_iso(nxt) > _parse_iso(now_iso):
            break
        fires.append(nxt)
        cursor = nxt
    return fires


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

class LocalScheduler:
    """宿主运行期间的本地调度器；clock/sleep 可注入（可控时钟）。

    runner：触发时的执行回调 `(task, fire_key) -> (outcome, run_id, detail)`；
    缺省用 `default_runner`（对接 TASK-022 运行记录 + TASK-021 动作目录）。
    """

    def __init__(self, store: TaskStore, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 runner: Callable[[dict, str], tuple[str, str | None, str | None]] | None = None,
                 instance_id: str | None = None,
                 lease_ttl: float = DEFAULT_LEASE_TTL,
                 tick_interval: float = DEFAULT_TICK_INTERVAL) -> None:
        self.store = store
        self._clock = clock
        self._sleep = sleep
        self.runner = runner or default_runner
        self.instance_id = instance_id or ("sched-" + uuid.uuid4().hex[:8])
        self.lease_ttl = lease_ttl
        self.tick_interval = tick_interval
        self._stop = threading.Event()

    # -- 单次推进 -----------------------------------------------------------

    def tick(self, *, user_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """推进一次：获取租约 → 处理错过 → 触发到期任务。

        返回本次触发的记录列表（含 outcome；无租约时为空——双实例待命）。
        """
        now_ts = self._clock()
        if not self.store.acquire_lease("scheduler", self.instance_id,
                                        ttl=self.lease_ttl, now=now_ts):
            return []  # 另一实例持有：待命
        now_iso = _now_iso(self._clock)
        fired: list[dict[str, Any]] = []
        for task in self._due_tasks(user_ids):
            fired.extend(self._fire_task(task, now_iso))
        return fired

    def _due_tasks(self, user_ids: list[str] | None) -> list[dict[str, Any]]:
        tasks = self.store.all_enabled_tasks()
        if user_ids is not None:
            tasks = [t for t in tasks if t["user_id"] in user_ids]
        return tasks

    def _fire_task(self, task: dict[str, Any], now_iso: str) -> list[dict[str, Any]]:
        """触发一个任务：统一处理 (last, now] 之间的全部时点。

        语义：
        - 恰一个时点 → 正常触发（missed=False）
        - 多个时点 → 更早的仅记录 missed（不执行），**最近一个补发执行**
          （missed=True，标注这是错过后的补发，不逐条回放风暴）
        """
        results: list[dict[str, Any]] = []
        last = task.get("last_fired_at")
        if not last:
            # 首次运行：初始化 last_fired 为创建时刻，不补发历史
            created = task["created_at"]
            self.store.update_fire_state(
                task["id"], last_fired_at=created,
                next_fire_at=next_fire_after(task, created))
            return results
        fires = missed_fires(task, since_iso=last, now_iso=now_iso)
        if not fires:
            return results
        multiple = len(fires) > 1
        for i, fire_at in enumerate(fires):
            is_latest = i == len(fires) - 1
            fire_key = f"{task['id']}@{fire_at}"
            if not self.store.claim_fire(task["id"], fire_key, fired_at=now_iso,
                                         outcome="pending"):
                continue  # 该时点已处理过（幂等）
            if not is_latest:
                # 更早的错过：仅记录，不执行
                self.store.update_fire_outcome(
                    task["id"], fire_key, outcome="missed",
                    detail="错过（仅记录，不补发）")
                results.append({"task_id": task["id"], "fire_key": fire_key,
                                "outcome": "missed", "run_id": None,
                                "detail": "错过（仅记录，不补发）", "missed": True})
            else:
                outcome, run_id, detail = self.runner(task, fire_key)
                self.store.update_fire_outcome(
                    task["id"], fire_key, outcome=outcome,
                    run_id=run_id, detail=detail)
                results.append({"task_id": task["id"], "fire_key": fire_key,
                                "outcome": outcome, "run_id": run_id,
                                "detail": detail, "missed": multiple})
            self.store.update_fire_state(
                task["id"], last_fired_at=fire_at,
                next_fire_at=next_fire_after(task, fire_at))
        return results

    # -- 循环与预览 ---------------------------------------------------------

    def run_forever(self, *, user_ids: list[str] | None = None) -> None:
        """宿主运行期间循环推进；stop() 或进程退出即停。"""
        while not self._stop.is_set():
            self.tick(user_ids=user_ids)
            self._sleep(self.tick_interval)

    def stop(self) -> None:
        self._stop.set()

    def preview(self, user_id: str, task_id: str) -> str | None:
        """下次执行预览（界面用）。"""
        task = self.store.get_task(user_id, task_id)
        if task is None or not task["enabled"]:
            return None
        base = task.get("last_fired_at") or task["created_at"]
        return next_fire_after(task, base)


# ---------------------------------------------------------------------------
# 默认执行器：对接 TASK-022 运行记录 + TASK-021 动作目录
# ---------------------------------------------------------------------------

def default_runner(task: dict[str, Any], fire_key: str):
    """触发执行：重查身份与动作可用性；高影响写入无人确认不执行。

    Returns:
        (outcome, run_id, detail)。outcome ∈
        executed | waiting_confirmation | skipped | failed。
    """
    from src.agent.actions import ActionContext, actions_available, get_action

    # 触发时权限检查（复核项）：归属用户身份缺失 → 拒绝执行
    if not str(task.get("user_id") or "").strip():
        return "skipped", None, "任务缺少归属用户身份，拒绝执行。"
    action = get_action(task["action"])
    if action is None:
        return "skipped", None, f"动作不存在：{task['action']}"
    ctx = ActionContext(
        tools_enabled=os.getenv("AGENT_ENABLE_TOOLS", "0") == "1",
        exec_enabled=os.getenv("AGENT_ENABLE_EXEC_TOOL", "0") == "1",
        web_enabled=os.getenv("AGENT_ENABLE_WEB_TOOL", "0") == "1",
    )
    if action not in actions_available(ctx):
        return "skipped", None, f"动作当前不可用（开关未开）：{task['action']}"
    # 高影响写入：调度触发无交互通道 → 无人确认不执行（需人在场则等待）
    if task["action"] in _CONFIRMABLE:
        return "waiting_confirmation", None, "变更类动作需人在场确认，已等待"
    # 运行记录对接：同 (task, fire_key) 幂等（TASK-022）
    run_id = None
    store = None
    try:
        from src.agent.run_store import RunStore
        # 路径：OS_AGENT_RUNS_DB 环境变量优先（测试注入），缺省项目根相对
        store = RunStore()
        record, dup = store.begin_run(
            task["user_id"], "sched-" + uuid.uuid4().hex[:12], route="scheduler",
            idempotency_key=f"sched:{task['id']}:{fire_key}")
        run_id = record["id"]
        if dup:
            return "executed", run_id, "同一时点已有运行（幂等复用）"
        store.mark_running(task["user_id"], run_id)
        ok, content, error = _dispatch(task)
        status = "completed" if ok else "failed"
        store.finish_run(task["user_id"], run_id, status,
                         stop_reason=error, steps=[])
        return ("executed" if ok else "failed"), run_id, content[:120]
    except Exception as exc:  # noqa: BLE001 —— 调度失败不拖垮循环
        return "failed", run_id, f"触发失败：{type(exc).__name__}"
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def _dispatch(task: dict[str, Any]):
    """经统一动作目录执行（TASK-021）。"""
    from src.agent.actions import ActionContext, dispatch_action

    ctx = ActionContext(
        tools_enabled=os.getenv("AGENT_ENABLE_TOOLS", "0") == "1",
        exec_enabled=os.getenv("AGENT_ENABLE_EXEC_TOOL", "0") == "1",
        web_enabled=os.getenv("AGENT_ENABLE_WEB_TOOL", "0") == "1",
    )
    return dispatch_action(task["action"], task["args"], ctx)
