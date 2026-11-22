#!/usr/bin/env python3
"""规模与并发基线 — TASK-006 / Issue #5（优化前后可复跑对比）.

对 memory_stats / get_preference_profile / recall(词面补召) 三条热点路径，
在固定虚构数据上测量 100 / 1k / 10k 规模与多用户并发下的：
  - 计数正确性（memory_stats 各层与总量 vs 数据集真值）
  - 画像正确性（global 与 scene 画像的 kv vs 数据集真值）
  - P50 / P95 / 平均延迟（tracemalloc 峰值内存增量）
  - 吞吐（并发阶段 ops/s）与线程错误（线程安全事件如实记录）
  - 后端调用次数（计数代理包装存储客户端）
  - 截断行为（profile top_k=10_000 边界：过滤后超出即静默丢键）

方法学声明：本基线使用 InMemoryMilvusClient + 确定性 hash embedding，
只验证链路与口径、采集优化前的证据基线；不代表 Windows 目标存储与模型的
真实延迟与吞吐（目标配置见 run_benchmarks.py --real 与 TASK-007）。

运行:
    python benchmarks/run_scale_baseline.py                      # 默认三档规模
    python benchmarks/run_scale_baseline.py --scales 100,1000    # 自定义规模
    python benchmarks/run_scale_baseline.py --reps 6 --workers 4,8

输出: benchmarks/results/scale_baseline_<ts>.json / .md
退出码: 全部正确性断言通过 → 0；任何计数/画像不符或并发错误 → 1
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

RESULTS_DIR = _ROOT / "benchmarks" / "results"

PREFERENCE_RATIO = 0.4  # 每用户配额中偏好记录占比（其余为知识）
SUPERSEDED_KNOWLEDGE_RATIO = 0.1  # 知识中 is_latest=False 的占比
VERSION_DEPTH = 3  # 版本链深度（仅链尾 is_latest=True）


def _perf() -> float:
    return time.perf_counter()


# ---------------------------------------------------------------------------
# 固定虚构数据集（种子可复现；真值模型用于正确性断言）
# ---------------------------------------------------------------------------


def _content(rng: random.Random, tag: str, index: int) -> str:
    """生成 120~600 字符的确定性伪正文，嵌入用户/键标记便于排查。"""
    fillers = [
        "系统偏好记录", "办公助手上下文", "终端命令输出", "文档片段摘要",
        "项目配置快照", "用户操作行为", "服务日志片段", "工具调用结果",
    ]
    body = "；".join(rng.choice(fillers) for _ in range(rng.randint(6, 24)))
    return f"[{tag}#{index}] {body}"[:600]


def build_dataset(
    *,
    total: int,
    users: list[str],
    seed: int = 42,
    version_depth: int = VERSION_DEPTH,
) -> tuple[list[dict], dict[str, dict]]:
    """构造固定虚构数据集并返回 (records, truth)。

    truth[user] = {
        "n_total": int, "n_preference": int, "n_knowledge": int,
        "latest_kv": {key: value},          # 全部 is_latest=True 的偏好
        "scene_keys": [key, ...],           # office 场景键（与全局键不相交）
    }
    键范围设计：全局键 / 场景键 / 自定义键互不相交，保证画像真值与
    遍历顺序无关（可在任意后端上复现）。
    """
    from src.preference.keys import PREFERENCE_KEYS

    rng = random.Random(seed)
    stable_keys = sorted(PREFERENCE_KEYS)
    per_user = max(total // len(users), 1)
    records: list[dict] = []
    truth: dict[str, dict] = {}

    for u_index, user_id in enumerate(users):
        quota = per_user if u_index < len(users) - 1 else max(total - per_user * (len(users) - 1), 1)
        n_pref = max(int(quota * PREFERENCE_RATIO), 1)
        n_know = max(quota - n_pref, 0)

        # 键切片：全局 / 场景 / 自定义三段互不相交（跨用户复用同一键空间）
        g_count = max(int(n_pref * 0.7), 1)
        s_count = max(int(n_pref * 0.2), 1)
        c_count = max(n_pref - g_count - s_count, 0)
        global_keys = stable_keys[: min(g_count, len(stable_keys))]
        scene_keys = [
            f"habit.scene_office_{i}" for i in range(s_count)
        ]
        custom_keys = [
            f"habit.custom_u{u_index}_{i}" for i in range(c_count)
        ]

        latest_kv: dict[str, str] = {}
        n_pref_records = 0
        for k_index, key in enumerate(global_keys + scene_keys + custom_keys):
            is_scene = k_index >= len(global_keys) and k_index < len(global_keys) + len(scene_keys)
            depth = version_depth if k_index % 3 == 0 else 1  # 1/3 的键带版本链
            value = f"{user_id}-v{depth}-{rng.choice(['on', 'off', 'auto', 'cn', 'd盘'])}"
            for v in range(1, depth + 1):
                records.append({
                    "memory_id": f"{user_id}-p{k_index}-v{v}",
                    "content": _content(rng, f"P{user_id[:6]}.{key}", v),
                    "user_id": user_id,
                    "memory_type": "preference",
                    "is_deleted": False,
                    "is_latest": v == depth,
                    "version": v,
                    "preference_key": key,
                    "preference_value": value if v == depth else f"{user_id}-v{v}-old",
                    "scope": "scene" if is_scene else "global",
                    "scene_id": "office" if is_scene else None,
                    "confidence": round(0.6 + 0.05 * min(depth, 8), 4),
                    "confidence_source": "dataset",
                    "n_observations": depth,
                    "category": key.split(".", 1)[0],
                    "source_type": "behavior",
                })
                n_pref_records += 1
            latest_kv[key] = value

        n_know_records = 0
        know_types = ["tool_call", "document_chunk", "project_context", "feedback"]
        for k_index in range(n_know):
            superseded = rng.random() < SUPERSEDED_KNOWLEDGE_RATIO
            records.append({
                "memory_id": f"{user_id}-k{k_index}",
                "content": _content(rng, f"K{user_id[:6]}", k_index),
                "user_id": user_id,
                "memory_type": know_types[k_index % len(know_types)],
                "is_deleted": False,
                "is_latest": not superseded,
                "version": 1,
                "scene_id": None,
                "confidence": 0.9,
                "source_type": "tool_result",
            })
            n_know_records += 1

        truth[user_id] = {
            "n_total": n_pref_records + n_know_records,
            "n_preference": n_pref_records,
            "n_knowledge": n_know_records,
            "latest_kv": latest_kv,
            "scene_keys": scene_keys,
        }

    return records, truth


def insert_records(client, records: list[dict]) -> None:
    """把虚构记录直接写入存储客户端（绕过编排管线，聚焦读取路径）。"""
    for record in records:
        client.insert(
            memory_id=record["memory_id"],
            content=record["content"],
            embedding=[0.0] * 48,
            metadata={k: v for k, v in record.items()
                      if k not in {"memory_id", "content"}},
        )


class CountingClient:
    """计数代理：按方法统计存储适配器调用次数（后端调用次数口径）。"""

    TRACKED = ("search", "count", "get_by_id", "update", "delete", "delete_by_filters")

    def __init__(self, inner):
        self._inner = inner
        self.calls = {name: 0 for name in self.TRACKED}
        self._lock = threading.Lock()

    def _tick(self, name: str):
        with self._lock:
            self.calls[name] = self.calls.get(name, 0) + 1

    def search(self, *args, **kwargs):
        self._tick("search")
        return self._inner.search(*args, **kwargs)

    def count(self, *args, **kwargs):
        self._tick("count")
        return self._inner.count(*args, **kwargs)

    def get_by_id(self, *args, **kwargs):
        self._tick("get_by_id")
        return self._inner.get_by_id(*args, **kwargs)

    def update(self, *args, **kwargs):
        self._tick("update")
        return self._inner.update(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._tick("delete")
        return self._inner.delete(*args, **kwargs)

    def delete_by_filters(self, *args, **kwargs):
        self._tick("delete_by_filters")
        return self._inner.delete_by_filters(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _calls_delta(client: CountingClient, before: dict[str, int]) -> dict[str, float]:
    """按方法返回调用次数增量，并归一化为每次调用的均值由调用方处理。"""
    return {name: client.calls.get(name, 0) - before.get(name, 0) for name in client.calls}


# ---------------------------------------------------------------------------
# 测量工具
# ---------------------------------------------------------------------------


def _measure(fn, *, reps: int, warmup: int = 2) -> dict:
    """重复执行 fn，采集延迟（P50/P95/均值）与 tracemalloc 峰值增量。"""
    for _ in range(warmup):
        fn()
    latencies: list[float] = []
    peak_alloc = 0
    for _ in range(reps):
        tracemalloc.start()
        start = _perf()
        fn()
        latencies.append((_perf() - start) * 1000)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_alloc = max(peak_alloc, peak)

    ordered = sorted(latencies)
    return {
        "reps": reps,
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(ordered[max(len(ordered) - 1 - int(len(ordered) * 0.05), 0)], 3),
        "mean_ms": round(statistics.fmean(latencies), 3),
        "min_ms": round(min(latencies), 3),
        "peak_alloc_delta_mb": round(peak_alloc / (1024 * 1024), 3),
    }


def _format_calls(delta: dict[str, float]) -> str:
    parts = [f"{name}×{value}" for name, value in delta.items() if value]
    return " ".join(parts) if parts else "0"


# ---------------------------------------------------------------------------
# 单规模评测
# ---------------------------------------------------------------------------


def run_scale(
    scale: int,
    *,
    users: list[str],
    seed: int = 42,
    reps: int = 8,
    warmup: int = 2,
    concurrency_workers: list[int] | None = None,
    concurrency_rounds: int = 3,
) -> dict:
    from src.core.orchestrator import MemoryOrchestrator
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    records, truth = build_dataset(total=scale, users=users, seed=seed)
    client = CountingClient(InMemoryMilvusClient())
    insert_records(client, records)
    orchestrator = MemoryOrchestrator(milvus=client)
    errors: list[str] = []

    # ---- 计数正确性 ----
    stats_checks: dict[str, bool] = {}
    for user_id, expected in truth.items():
        stats = orchestrator.memory_stats(user_id)
        stats_checks[f"count:{user_id}"] = (
            stats.milvus.count == expected["n_total"]
            and stats.milvus.preference_count == expected["n_preference"]
            and stats.milvus.knowledge_count == expected["n_knowledge"]
            and stats.total_count == expected["n_total"]
        )

    # ---- 画像正确性（global 与 office 场景）----
    profile_checks: dict[str, bool] = {}
    for user_id, expected in truth.items():
        profile = orchestrator.get_preference_profile(user_id)
        profile_checks[f"kv:{user_id}"] = (
            profile["kv"] == expected["latest_kv"]
            and profile["n_features"] == len(expected["latest_kv"])
        )
        office = orchestrator.get_preference_profile(user_id, scene_id="office")
        profile_checks[f"kv_scene:{user_id}"] = (
            office["kv"] == expected["latest_kv"]
        )

    # ---- 延迟 / 内存 / 后端调用（按方法记录，归一化为每次调用的均值）----
    probe_user = users[0]
    stats_ops = reps + warmup
    calls_before = dict(client.calls)
    stats_latency = _measure(lambda: orchestrator.memory_stats(probe_user), reps=reps, warmup=warmup)
    stats_delta = {
        name: round((value - calls_before.get(name, 0)) / stats_ops, 2)
        for name, value in client.calls.items()
    }
    calls_before = dict(client.calls)
    profile_latency = _measure(lambda: orchestrator.get_preference_profile(probe_user), reps=reps, warmup=warmup)
    profile_delta = {
        name: round((value - calls_before.get(name, 0)) / stats_ops, 2)
        for name, value in client.calls.items()
    }

    recall_query = f"如何部署罕见关键词 qxyz{scale} 并排查超时"  # 含未命中词面 → 触发词面补召
    recall_ops = max(reps - 2, 3) + 1
    calls_before = dict(client.calls)
    recall_latency = _measure(
        lambda: orchestrator.recall(
            __import__("src.common.models", fromlist=["RetrievalQuery"]).RetrievalQuery(
                query=recall_query, user_id=probe_user, top_k=5,
            )
        ),
        reps=max(reps - 2, 3),
        warmup=1,
    )
    recall_delta = {
        name: round((value - calls_before.get(name, 0)) / recall_ops, 2)
        for name, value in client.calls.items()
    }
    strategy = orchestrator.recall(
        __import__("src.common.models", fromlist=["RetrievalQuery"]).RetrievalQuery(
            query=recall_query, user_id=probe_user, top_k=5,
        )
    ).strategy

    # ---- 多用户并发 ----
    concurrency: dict = {"configs": []}
    for workers in (concurrency_workers or [4]):
        latencies: list[float] = []
        thread_errors: list[str] = []
        lock = threading.Lock()

        def worker(wid: int) -> None:
            for r in range(concurrency_rounds):
                u = users[(wid + r) % len(users)]
                start = _perf()
                try:
                    orchestrator.memory_stats(u)
                    orchestrator.get_preference_profile(u)
                except Exception as exc:  # noqa: BLE001 - 并发事件如实记录
                    with lock:
                        thread_errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
                with lock:
                    latencies.append((_perf() - start) * 1000)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
        wall_start = _perf()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall_ms = (_perf() - wall_start) * 1000
        ops = len(latencies)
        concurrency["configs"].append({
            "workers": workers,
            "rounds": concurrency_rounds,
            "ops": ops,
            "wall_ms": round(wall_ms, 1),
            "ops_per_sec": round(ops / (wall_ms / 1000), 2) if wall_ms else 0.0,
            "p50_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
            "p95_ms": round(sorted(latencies)[max(len(latencies) - 1, 0)], 3) if latencies else 0.0,
            "n_errors": len(thread_errors),
            "error_samples": thread_errors[:3],
        })
        errors.extend(thread_errors)

    return {
        "scale": scale,
        "users": users,
        "n_records": len(records),
        "correctness": {
            "stats_checks": stats_checks,
            "profile_checks": profile_checks,
            "all_ok": all(stats_checks.values()) and all(profile_checks.values()) and not errors,
        },
        "latency": {
            "memory_stats": stats_latency,
            "get_preference_profile": profile_latency,
            "recall_lexical": recall_latency,
        },
        "backend_calls_per_op": {
            "memory_stats": stats_delta,
            "get_preference_profile": profile_delta,
            "recall_lexical": recall_delta,
            "recall_strategy": strategy,
        },
        "concurrency": concurrency,
        "errors": errors[:5],
    }


# ---------------------------------------------------------------------------
# 截断探针（超上限不得静默漏数的证据）
# ---------------------------------------------------------------------------


def run_truncation_probe(*, total_latest: int = 10_500, seed: int = 7) -> dict:
    """构造 total_latest 条 is_latest=True 的单用户偏好（键互不相同）。

    profile 路径 top_k=10_000：实现为「先过滤后截断」，过滤后超出 top_k
    的最新记录不会进入画像。Phase 2 后截断不再静默——画像显式携带
    truncated / n_latest_available / profile_top_k 字段（TASK-006）。
    """
    from src.core.orchestrator import MemoryOrchestrator
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    user_id = "probe_truncation_user"
    rng = random.Random(seed)
    records = []
    for i in range(total_latest):
        key = f"habit.custom_boundary_{i:05d}"
        records.append({
            "memory_id": f"probe-{i:05d}",
            "content": _content(rng, "PROBE", i),
            "user_id": user_id,
            "memory_type": "preference",
            "is_deleted": False,
            "is_latest": True,
            "version": 1,
            "preference_key": key,
            "preference_value": f"value-{i:05d}",
            "scope": "global",
            "scene_id": None,
            "confidence": 0.9,
            "n_observations": 1,
            "category": "habit",
            "source_type": "behavior",
        })

    client = InMemoryMilvusClient()
    insert_records(client, records)
    orchestrator = MemoryOrchestrator(milvus=client)

    profile = orchestrator.get_preference_profile(user_id)
    observed_keys = profile["n_features"]
    # stats 路径走 count 聚合：本规模应精确计数
    stats = orchestrator.memory_stats(user_id)
    return {
        "total_latest_preferences": total_latest,
        "profile_top_k": profile.get("profile_top_k"),
        "expected_features": total_latest,
        "observed_features": observed_keys,
        "missed_keys_beyond_top_k": max(total_latest - observed_keys, 0),
        "truncated_flag": profile.get("truncated"),
        "n_latest_available": profile.get("n_latest_available"),
        "stats_count": stats.milvus.count,
        "stats_exact": stats.milvus.count == total_latest,
        "note": "Phase 2 后截断显式暴露（truncated/n_latest_available）；"
                "调用方可据此分页或提示，不再静默漏数",
    }


# ---------------------------------------------------------------------------
# 环境与报告
# ---------------------------------------------------------------------------


def _git_commit() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(_ROOT),
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _render_md(report: dict) -> str:
    lines = [
        "# 规模与并发基线（TASK-006 / Issue #5）",
        "",
        f"- 生成时间：{report['meta']['generated_at']}",
        f"- 后端：{report['meta']['backend']}（仅接线证据，目标配置性能另测）",
        f"- Python：{report['environment']['python_version']} / 提交：{report['environment'].get('git_commit') or '未知'}",
        "",
    ]
    for section in report["scales"]:
        lat = section["latency"]
        calls = section["backend_calls_per_op"]
        lines.append(f"## 规模 {section['scale']}（{section['n_records']} 条记录，{len(section['users'])} 用户）")
        lines.append("")
        lines.append("| 路径 | P50(ms) | P95(ms) | 均值(ms) | 峰值内存增量(MB) | 后端调用/次 |")
        lines.append("|---|---|---|---|---|---|")
        for name, label in (
            ("memory_stats", "memory_stats"),
            ("get_preference_profile", "get_preference_profile"),
            ("recall_lexical", f"recall 词面补召（{calls.get('recall_strategy', '?')}）"),
        ):
            m = lat[name]
            call_note = _format_calls(calls.get(name, {})) if isinstance(calls.get(name), dict) else calls.get(name, "-")
            lines.append(f"| {label} | {m['p50_ms']} | {m['p95_ms']} | {m['mean_ms']} | "
                         f"{m['peak_alloc_delta_mb']} | {call_note} |")
        ok = section["correctness"]["all_ok"]
        lines.append("")
        lines.append(f"- 计数/画像正确性：{'全部通过' if ok else '存在不一致（见 JSON）'}")
        for cfg in section["concurrency"]["configs"]:
            lines.append(f"- 并发 {cfg['workers']} 线程 × {cfg['rounds']} 轮："
                         f"{cfg['ops']} 次 ops，{cfg['ops_per_sec']} ops/s，"
                         f"P50={cfg['p50_ms']}ms / P95={cfg['p95_ms']}ms，错误 {cfg['n_errors']}")
            for sample in cfg["error_samples"]:
                lines.append(f"  - 错误样例：{sample}")
        lines.append("")

    probe = report.get("truncation_probe")
    if probe:
        lines.append("## 截断探针（超上限不得静默漏数）")
        lines.append("")
        lines.append(f"- 最新偏好 {probe['total_latest_preferences']} 条（键互不相同），profile top_k={probe.get('profile_top_k')}")
        lines.append(f"- 期望特征数 {probe['expected_features']}，实际 {probe['observed_features']}，"
                     f"超出 top_k {probe.get('missed_keys_beyond_top_k')} 个键")
        lines.append(f"- 截断显式暴露：truncated={probe.get('truncated_flag')}，"
                     f"n_latest_available={probe.get('n_latest_available')}（不再静默）")
        lines.append(f"- stats 计数：{probe['stats_count']}（精确：{probe['stats_exact']}）")
        lines.append("")
    lines.append("> 局限性：InMemoryMilvusClient + hash 向量，仅验证链路与口径；"
                 "延迟/吞吐/内存为优化前基线证据，真实后端另行测量（TASK-007）。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="MOKOA 规模与并发基线（TASK-006 Phase 1）")
    parser.add_argument("--scales", type=str, default="100,1000,10000", help="逗号分隔的规模档位")
    parser.add_argument("--users", type=int, default=4, help="模拟用户数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reps", type=int, default=8, help="每条路径延迟重复次数")
    parser.add_argument("--workers", type=str, default="4", help="逗号分隔的并发线程档位")
    parser.add_argument("--rounds", type=int, default=3, help="并发轮数")
    parser.add_argument("--no-probe", action="store_true", help="跳过截断探针")
    parser.add_argument("--out", type=str, default="benchmarks/results")
    args = parser.parse_args()

    users = [f"scale_user_{i}" for i in range(max(args.users, 1))]
    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    workers = [int(w) for w in args.workers.split(",") if w.strip()]

    report: dict = {
        "meta": {
            "project": "麒忆智脑 / MOKOA",
            "task": "TASK-006 / Issue #5（规模与并发基线，优化前后可复跑对比）",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "backend": "InMemoryMilvusClient + hash embedding（接线证据）",
        },
        "environment": {
            "python_version": platform.python_version(),
            "git_commit": _git_commit(),
        },
        "scales": [],
        "truncation_probe": None,
    }

    all_ok = True
    for scale in scales:
        print(f"[scale] n={scale}, users={len(users)}, reps={args.reps} ...", flush=True)
        section = run_scale(
            scale,
            users=users,
            seed=args.seed,
            reps=args.reps,
            concurrency_workers=workers,
            concurrency_rounds=args.rounds,
        )
        report["scales"].append(section)
        all_ok = all_ok and section["correctness"]["all_ok"]
        lat = section["latency"]
        print(f"        stats P50={lat['memory_stats']['p50_ms']}ms  "
              f"profile P50={lat['get_preference_profile']['p50_ms']}ms  "
              f"ok={section['correctness']['all_ok']}", flush=True)

    if not args.no_probe:
        print("[probe] profile top_k 截断探针 ...", flush=True)
        probe = run_truncation_probe()
        report["truncation_probe"] = probe
        print(f"        超出 top_k 键数 = {probe['missed_keys_beyond_top_k']}, "
              f"truncated 标志 = {probe['truncated_flag']}", flush=True)

    out_dir = _ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"scale_baseline_{ts}.json"
    md_path = out_dir / f"scale_baseline_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(report), encoding="utf-8")
    print(f"\n报告已写入：\n  {json_path}\n  {md_path}", flush=True)

    if not all_ok:
        print("正确性断言存在失败：见报告 JSON 的 correctness 字段", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
