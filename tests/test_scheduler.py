"""TASK-031 / Issue #39 本地调度器回归。

对应验收：
- 可控时钟测睡眠唤醒、重启、夏令时重复/跳时及两个调度实例竞争
- 单次时点只创建一条运行（幂等去重）
- 高影响写入无人确认不执行（waiting_confirmation）
- 错过执行在重启后可见（fire_log 记录 missed）
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from src.agent.scheduler import (
    LocalScheduler,
    TaskStore,
    missed_fires,
    next_fire_after,
)


class FakeClock:
    """可控时钟：测试推进时间驱动调度。"""

    def __init__(self, start: datetime) -> None:
        self.ts = start.timestamp()

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds: float) -> None:
        self.ts += seconds

    def iso(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).astimezone().isoformat(
            timespec="seconds")


@pytest.fixture
def store(tmp_path):
    s = TaskStore(tmp_path / "scheduler.db")
    yield s
    s.close()


def _runner_records(calls: list):
    def runner(task, fire_key):
        calls.append((task["id"], fire_key))
        return "executed", f"run-{len(calls)}", None
    return runner


# ---------------------------------------------------------------------------
# 触发计算：一次/周期/每日 + 预览
# ---------------------------------------------------------------------------

def test_once_and_interval_and_preview(store) -> None:
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    task = store.create_task("u_a", "calculate", {"expression": "1+1"},
                             schedule_kind="once",
                             schedule={"at": "2026-11-26T18:00:00+08:00"},
                             now=clock.iso())
    assert next_fire_after(task, clock.iso()) is not None
    # 预览
    sched = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                           runner=_runner_records([]))
    assert sched.preview("u_a", task["id"])

    # interval：每 60 秒
    t2 = store.create_task("u_a", "calculate", {"expression": "2+2"},
                           schedule_kind="interval",
                           schedule={"every_seconds": 60, "start": clock.iso()},
                           now=clock.iso())
    nxt = next_fire_after(t2, clock.iso())
    assert nxt is not None
    delta = datetime.fromisoformat(nxt).timestamp() - clock.ts
    assert 0 < delta <= 60


def test_dst_spring_forward_shifts_and_fall_back_fires_once() -> None:
    """夏令时：跳时顺延到有效时刻；重复时刻只触发一次。"""
    # spring-forward：2026-03-08 02:30 America/New_York 不存在 → 03:30
    task = {"schedule_kind": "daily",
            "schedule": {"time": "02:30", "tz": "America/New_York"}}
    nxt = next_fire_after(task, "2026-03-08T00:00:00-05:00")
    assert nxt == "2026-03-08T03:30:00-04:00", "跳时应顺延到下一有效时刻"

    # fall-back：2026-11-01 01:30 出现两次 → 只取第一次（EDT）
    task2 = {"schedule_kind": "daily",
             "schedule": {"time": "01:30", "tz": "America/New_York"}}
    first = next_fire_after(task2, "2026-11-01T00:00:00-04:00")
    assert first == "2026-11-01T01:30:00-04:00"
    # 第一次之后直接到次日（第二次 01:30 EST 不重复触发）
    nxt2 = next_fire_after(task2, first)
    assert nxt2 == "2026-11-02T01:30:00-05:00", "重复时刻不得双触发"


# ---------------------------------------------------------------------------
# 可控时钟：睡眠唤醒 / 重启 / 错过执行
# ---------------------------------------------------------------------------

def test_sleep_wake_catches_up_latest_missed_once(store) -> None:
    """睡眠唤醒：错过多个时点 → 最近一次补发执行、更早仅记录 missed。"""
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    task = store.create_task("u_a", "calculate", {"expression": "1+1"},
                             schedule_kind="interval",
                             schedule={"every_seconds": 60, "start": clock.iso()},
                             now=clock.iso())
    calls: list = []
    sched = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                           runner=_runner_records(calls))
    sched.tick()  # 初始化 last_fired
    clock.advance(300)  # 睡眠 5 分钟：错过 5 个时点
    fired = sched.tick()
    missed = [f for f in fired if f["missed"]]
    assert len(missed) >= 4, f"应记录多个错过时点：{fired}"
    executed = [f for f in missed if f["outcome"] == "executed"]
    assert len(executed) == 1, "仅最近一次错过补发执行（不风暴）"
    assert len(calls) == 1
    history = store.fire_history("u_a", task["id"])
    assert sum(1 for h in history if h["outcome"] == "missed") >= 3


def test_restart_sees_missed_and_does_not_duplicate(store) -> None:
    """重启：新实例（同库）可见错过并补发；同一时点不重复执行。"""
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    store.create_task("u_a", "calculate", {"expression": "1+1"},
                      schedule_kind="interval",
                      schedule={"every_seconds": 60, "start": clock.iso()},
                      now=clock.iso())
    calls1: list = []
    sched1 = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                            runner=_runner_records(calls1), instance_id="s1")
    sched1.tick()
    clock.advance(180)  # 宿主退出期间错过 3 个时点

    # 重启：新实例接管（同 store）
    calls2: list = []
    sched2 = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                            runner=_runner_records(calls2), instance_id="s2")
    fired = sched2.tick()
    assert any(f["missed"] for f in fired), "重启后应可见错过"
    assert len(calls2) == 1, "补发最近一次"
    # 再次 tick：同一时点不重复
    again = sched2.tick()
    assert [f for f in again if not f["missed"]] == [], "同刻不重复触发"


def test_single_fire_creates_one_run_per_timepoint(store) -> None:
    """单次时点只创建一条运行：重复 tick 与双实例竞争下均幂等。"""
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    store.create_task("u_a", "calculate", {"expression": "1+1"},
                      schedule_kind="interval",
                      schedule={"every_seconds": 60, "start": clock.iso()},
                      now=clock.iso())
    calls: list = []
    sched = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                           runner=_runner_records(calls))
    sched.tick()
    clock.advance(60)
    first = sched.tick()
    second = sched.tick()  # 同时刻重复推进
    assert len([f for f in first if not f["missed"]]) == 1
    assert second == [], "同一时点重复 tick 不得再触发"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 双实例竞争（租约）
# ---------------------------------------------------------------------------

def test_two_instances_compete_via_lease(store) -> None:
    """两个调度实例：仅租约持有者触发；过期后另一实例接管。"""
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    store.create_task("u_a", "calculate", {"expression": "1+1"},
                      schedule_kind="interval",
                      schedule={"every_seconds": 60, "start": clock.iso()},
                      now=clock.iso())
    calls_a: list = []
    calls_b: list = []
    a = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                       runner=_runner_records(calls_a), instance_id="A",
                       lease_ttl=300)
    b = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                       runner=_runner_records(calls_b), instance_id="B",
                       lease_ttl=300)
    a.tick()  # A 获得租约（初始化 last_fired）
    clock.advance(60)
    a.tick()  # A 触发到期时点
    assert len(calls_a) == 1
    b.tick()  # B 无租约（A 的租约未过期）→ 待命
    assert calls_b == [], "非持有者不得触发"

    # A 崩溃：租约过期后 B 接管
    clock.advance(400)  # 超过 lease_ttl
    fired_b = b.tick()
    assert fired_b, "租约过期后 B 应接管触发"
    assert calls_b, "B 接管后执行"


# ---------------------------------------------------------------------------
# 高影响写入无人确认不执行 / 暂停删除 / 运行对接
# ---------------------------------------------------------------------------

def test_high_impact_write_waits_without_confirmation(store, monkeypatch) -> None:
    """变更类动作在调度触发（无交互通道）→ waiting_confirmation 不执行。"""
    monkeypatch.setenv("AGENT_ENABLE_TOOLS", "1")
    monkeypatch.setenv("AGENT_EXEC_CWD", str(store.path.parent))
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    store.create_task("u_a", "write_file", {"path": "out.txt", "content": "x"},
                      schedule_kind="interval",
                      schedule={"every_seconds": 60, "start": clock.iso()},
                      now=clock.iso())
    from src.agent.scheduler import default_runner
    sched = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                           runner=default_runner)
    sched.tick()
    clock.advance(60)
    fired = sched.tick()
    outcomes = [f["outcome"] for f in fired if not f["missed"]]
    assert outcomes == ["waiting_confirmation"], f"无人确认不得执行：{fired}"
    assert not (store.path.parent / "out.txt").exists()


def test_default_runner_executes_readonly_and_records_run(store, monkeypatch) -> None:
    """只读动作经默认执行器执行，并对接 TASK-022 运行记录（幂等键）。"""
    monkeypatch.setenv("AGENT_ENABLE_TOOLS", "1")
    monkeypatch.setenv("OS_AGENT_RUNS_DB", str(store.path.parent / "runs.db"))
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    store.create_task("u_a", "calculate", {"expression": "6*7"},
                      schedule_kind="interval",
                      schedule={"every_seconds": 60, "start": clock.iso()},
                      now=clock.iso())
    from src.agent.scheduler import default_runner
    sched = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                           runner=default_runner)
    sched.tick()
    clock.advance(60)
    fired = sched.tick()
    executed = [f for f in fired if f["outcome"] == "executed"]
    assert executed and executed[0]["run_id"], "应创建运行记录"
    # 运行记录可查（TASK-022）
    from src.agent.run_store import RunStore
    runs = RunStore(project_root=str(store.path.parent))
    record = runs.get_run("u_a", executed[0]["run_id"])
    assert record is not None and record["route"] == "scheduler"
    assert record["status"] == "completed"
    runs.close()


def test_pause_resume_delete(store) -> None:
    """暂停后不触发；恢复后继续；删除后消失。"""
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    task = store.create_task("u_a", "calculate", {"expression": "1"},
                             schedule_kind="interval",
                             schedule={"every_seconds": 60, "start": clock.iso()},
                             now=clock.iso())
    calls: list = []
    sched = LocalScheduler(store, clock=clock, sleep=lambda s: None,
                           runner=_runner_records(calls))
    sched.tick()
    assert store.set_enabled("u_a", task["id"], False) is True
    clock.advance(120)
    assert sched.tick() == [], "暂停任务不触发"
    store.set_enabled("u_a", task["id"], True)
    clock.advance(60)
    assert sched.tick(), "恢复后继续触发"
    assert store.delete_task("u_a", task["id"]) is True
    assert store.get_task("u_a", task["id"]) is None
    assert store.delete_task("u_b", task["id"]) is False  # 跨用户不可删


def test_user_isolation(store) -> None:
    """任务按用户隔离：跨用户读/删不可见。"""
    clock = FakeClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
    task = store.create_task("u_a", "calculate", {"expression": "1"},
                             schedule_kind="once",
                             schedule={"at": "2026-11-27T09:00:00+08:00"},
                             now=clock.iso())
    assert store.get_task("u_b", task["id"]) is None
    assert store.list_tasks("u_b") == []
    assert store.fire_history("u_b", task["id"]) == []
