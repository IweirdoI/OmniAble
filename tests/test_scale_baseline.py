"""规模与并发基线 harness 的正确性回归（TASK-006 / Issue #5）.

Module: testing | Requirement: (7) 量化评测

覆盖：
  - 固定虚构数据集的真值一致性（记录数 / is_latest 唯一性 / 键域不相交）
  - 单规模评测的计数与画像正确性断言、报告字段完整性
  - 截断探针：profile top_k=10_000 边界处的静默漏数必须被量化（不得静默）
  - 并发阶段在 InMemory 后端上的线程安全（错误如实记录，本口径下应为 0）

说明：全部使用 InMemoryMilvusClient + hash 向量，仅验证 harness 与链路口径，
不代表 Milvus / 麒麟 SDK 性能。
"""

from __future__ import annotations

import pytest

from benchmarks.run_scale_baseline import (
    build_dataset,
    run_scale,
    run_truncation_probe,
)


def test_build_dataset_truth_consistency() -> None:
    """数据集真值与记录一致：总数、版本链唯一最新、键域不相交。"""
    users = ["truth_user_0", "truth_user_1", "truth_user_2"]
    records, truth = build_dataset(total=120, users=users, seed=11)

    assert len(records) == sum(item["n_total"] for item in truth.values())

    for user_id, expected in truth.items():
        user_records = [r for r in records if r["user_id"] == user_id]
        assert len(user_records) == expected["n_total"]
        prefs = [r for r in user_records if r["memory_type"] == "preference"]
        assert len(prefs) == expected["n_preference"]
        assert len(user_records) - len(prefs) == expected["n_knowledge"]

        # 每个键恰好一条 is_latest=True（版本链唯一最新）
        latest_keys = [r["preference_key"] for r in prefs if r["is_latest"]]
        assert len(latest_keys) == len(set(latest_keys))
        assert set(latest_keys) == set(expected["latest_kv"])

        # 全局键与场景键不相交（画像真值与遍历顺序无关）
        global_keys = {r["preference_key"] for r in prefs if r["scope"] == "global"}
        scene_keys = {r["preference_key"] for r in prefs if r["scope"] == "scene"}
        assert not (global_keys & scene_keys)
        assert set(expected["scene_keys"]) == scene_keys

        # 场景记录都挂在 office 场景下
        assert all(
            r["scene_id"] == "office"
            for r in prefs
            if r["preference_key"] in scene_keys
        )


def test_run_scale_small_scale_correctness() -> None:
    """单规模评测：计数/画像正确性全部通过，报告字段完整。"""
    section = run_scale(
        120,
        users=["scale_u0", "scale_u1", "scale_u2"],
        seed=13,
        reps=2,
        warmup=0,
        concurrency_workers=[2],
        concurrency_rounds=2,
    )

    correctness = section["correctness"]
    assert correctness["all_ok"] is True, (
        f"计数/画像与真值不一致：stats={correctness['stats_checks']} "
        f"profile={correctness['profile_checks']}"
    )
    assert all(correctness["stats_checks"].values())
    assert all(correctness["profile_checks"].values())

    # 报告字段完整性（验收口径：P50/P95、峰值内存、后端调用次数、并发）
    for name in ("memory_stats", "get_preference_profile", "recall_lexical"):
        metric = section["latency"][name]
        assert {"p50_ms", "p95_ms", "mean_ms", "peak_alloc_delta_mb"} <= set(metric)
    assert set(section["backend_calls_per_op"]) >= {
        "memory_stats", "get_preference_profile", "recall_lexical",
    }
    # TASK-006 Phase 2：stats 走 count 聚合（不再全量拉取），profile 为投影 + count
    stats_calls = section["backend_calls_per_op"]["memory_stats"]
    assert stats_calls["search"] == 0, (
        f"优化后 memory_stats 不应全量拉取记录（实际 search={stats_calls['search']}）"
    )
    assert stats_calls["count"] == pytest.approx(2.0), (
        "memory_stats 每次调用应为 2 次 count 聚合（总数 + 偏好数）"
    )
    profile_calls = section["backend_calls_per_op"]["get_preference_profile"]
    assert profile_calls["search"] == pytest.approx(1.0)
    assert profile_calls["count"] == pytest.approx(0.0), (
        "小规模未达 top_k 上限，profile 走快速路径不做 count（边界情形才校验截断）"
    )

    config = section["concurrency"]["configs"][0]
    assert config["ops"] == config["workers"] * config["rounds"]
    assert config["n_errors"] == 0, f"并发阶段不应有线程错误：{config['error_samples']}"
    assert section["errors"] == []


def test_truncation_probe_detects_truncation_and_exposes_flag() -> None:
    """profile top_k=10_000 边界：超出部分不进画像，但必须显式暴露。

    在恰好 10_050 条互不相同键的 is_latest 偏好下，实现只能取回
    10_000 条（先过滤后截断）；Phase 2 后画像携带 truncated=True 与
    n_latest_available，不再静默漏数。
    """
    probe = run_truncation_probe(total_latest=10_050)

    assert probe["expected_features"] == 10_050
    assert probe["observed_features"] == 10_000, (
        "实现为『先过滤后 top_k 截断』；若该断言失败，说明截断行为已变化，"
        "需同步更新探针口径"
    )
    assert probe["missed_keys_beyond_top_k"] == 50
    assert probe["truncated_flag"] is True, "超上限必须显式暴露 truncated 标志"
    assert probe["n_latest_available"] == 10_050
    assert probe["stats_exact"] is True, "10_050 条应被 count 聚合精确计数"


def test_probe_small_scale_no_truncation() -> None:
    """低于 top_k 上限时不触发截断（探针口径的阴性对照）。"""
    probe = run_truncation_probe(total_latest=500)
    assert probe["observed_features"] == 500
    assert probe["missed_keys_beyond_top_k"] == 0
    assert probe["truncated_flag"] is False
    assert probe["n_latest_available"] == 500
