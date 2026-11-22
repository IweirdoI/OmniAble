"""memory_stats / get_preference_profile 优化后的正确性回归（TASK-006 / Issue #5）.

Module: testing | Requirement: (7) 量化评测

Phase 2 优化口径：
  - memory_stats 走 count 聚合，不再全量拉取记录
  - get_preference_profile 走字段投影（不取 content），并以 count 显式暴露
    top_k 截断（truncated / n_latest_available），不再静默漏数
  - 写入 / 版本更新 / 遗忘后画像与计数即时一致；跨用户不得串数据

说明：全部使用 InMemoryMilvusClient；MilvusClient.count 的 count(*) 输出
与麒麟向量引擎兼容性以真实后端实测为准（Windows 开发环境无 milvus_lite）。
"""

from __future__ import annotations

import pytest

from src.common.models import RetrievalQuery
from src.core.orchestrator import MemoryOrchestrator
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
from src.knowledge.memory_service.memory_schema import (
    MemoryEntry,
    MemorySource,
    MemoryTypeTag,
    TaskType,
)


class CountingClient(InMemoryMilvusClient):
    """记录调用方式：验证 stats/profile 走聚合与投影而非全量拉取。"""

    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[dict] = []
        self.count_calls: list[dict] = []

    def search(self, embedding, filters, top_k=5, *, output_fields=None):
        self.search_calls.append({"filters": filters, "output_fields": output_fields})
        return super().search(embedding, filters, top_k, output_fields=output_fields)

    def count(self, filters):
        self.count_calls.append({"filters": filters})
        return super().count(filters)


def _make_orchestrator() -> tuple[MemoryOrchestrator, CountingClient]:
    client = CountingClient()
    return MemoryOrchestrator(milvus=client), client


def _pref_entry(
    user_id: str,
    *,
    key: str,
    value: str,
    content: str,
    version: int = 1,
    supersedes: str | None = None,
) -> MemoryEntry:
    metadata: dict = {
        "preference_key": key,
        "preference_value": value,
        "version": version,
        "is_latest": True,
        "scope": "global",
        "confidence": 0.9,
        "n_observations": version,
        "confidence_source": "test",
    }
    if supersedes:
        metadata["supersedes_memory_id"] = supersedes
    return MemoryEntry(
        content=content,
        memory_type=MemoryTypeTag.PREFERENCE,
        task_type=TaskType.CODING,
        source=MemorySource.MANUAL,
        user_id=user_id,
        confidence=0.9,
        metadata=metadata,
    )


def test_memory_stats_uses_count_aggregation_without_full_pull() -> None:
    """stats 必须走 count 聚合，不得全量拉取记录；软删记录不计入。"""
    orchestrator, client = _make_orchestrator()
    client.insert(
        memory_id="s-pref-1",
        content="喜欢中文回答",
        embedding=[0.0] * 48,
        metadata={"user_id": "stats_user", "memory_type": "preference", "is_latest": True},
    )
    client.insert(
        memory_id="s-know-1",
        content="pip 超时换国内镜像",
        embedding=[0.0] * 48,
        metadata={"user_id": "stats_user", "memory_type": "tool_call", "is_latest": True},
    )
    client.insert(
        memory_id="s-know-2",
        content="已被软删的记录",
        embedding=[0.0] * 48,
        metadata={"user_id": "stats_user", "memory_type": "tool_call", "is_latest": True},
    )
    client.delete("s-know-2", soft=True)

    stats = orchestrator.memory_stats("stats_user")

    assert stats.milvus.count == 2
    assert stats.milvus.preference_count == 1
    assert stats.milvus.knowledge_count == 1
    assert stats.total_count == 2
    assert client.search_calls == [], "stats 不得全量拉取记录（count 聚合口径）"
    assert len(client.count_calls) == 2, "stats 应恰好 2 次 count（总数 + 偏好数）"


def test_profile_uses_projection_without_content() -> None:
    """画像查询必须使用字段投影且不取 content（元数据足够构建画像）。"""
    orchestrator, client = _make_orchestrator()
    client.insert(
        memory_id="proj-pref-1",
        content="以后文件都下载到 D 盘",
        embedding=[0.0] * 48,
        metadata={
            "user_id": "proj_user",
            "memory_type": "preference",
            "is_latest": True,
            "preference_key": "habit.download_path",
            "preference_value": "D 盘",
            "scope": "global",
            "version": 1,
            "confidence": 0.9,
            "n_observations": 1,
        },
    )

    profile = orchestrator.get_preference_profile("proj_user")

    assert profile["kv"] == {"habit.download_path": "D 盘"}
    assert client.search_calls, "画像应恰好一次投影查询"
    assert len(client.search_calls) == 1
    used_fields = client.search_calls[0]["output_fields"]
    assert used_fields is not None, "画像查询必须显式指定投影字段"
    assert "content" not in used_fields, "投影不得拉取大 content 字段"
    assert {"preference_key", "preference_value", "scope", "version"} <= set(used_fields)
    assert client.count_calls == [], "未达 top_k 上限时无需 count（避免第二次全量扫描）"
    assert profile["truncated"] is False


def test_profile_truncated_flag_flips_at_top_k_boundary() -> None:
    """超过 profile top_k 时 truncated=True；低于上限时为 False。"""
    orchestrator, client = _make_orchestrator()
    for i in range(300):
        client.insert(
            memory_id=f"flag-{i:04d}",
            content=f"偏好样本 {i}",
            embedding=[0.0] * 48,
            metadata={
                "user_id": "flag_user",
                "memory_type": "preference",
                "is_latest": True,
                "preference_key": f"habit.custom_flag_{i:04d}",
                "preference_value": f"v{i}",
                "scope": "global",
                "version": 1,
            },
        )

    profile = orchestrator.get_preference_profile("flag_user")
    assert profile["truncated"] is False
    assert profile["n_features"] == 300
    assert profile["n_latest_available"] == 300


def test_profile_reports_truncation_explicitly_beyond_top_k() -> None:
    """10_050 条互不相同键的最新偏好：取回 10_000 条，truncated=True。

    超出部分不进画像，但通过 truncated / n_latest_available 显式暴露，
    不再静默漏数（Issue #5「超上限不得静默漏数」）。
    """
    orchestrator, client = _make_orchestrator()
    total = 10_050
    for i in range(total):
        client.insert(
            memory_id=f"trunc-{i:05d}",
            content=f"边界样本 {i}",
            embedding=[0.0] * 48,
            metadata={
                "user_id": "trunc_user",
                "memory_type": "preference",
                "is_latest": True,
                "preference_key": f"habit.custom_boundary_{i:05d}",
                "preference_value": f"v{i}",
                "scope": "global",
                "version": 1,
            },
        )

    profile = orchestrator.get_preference_profile("trunc_user")
    assert profile["n_features"] == 10_000
    assert profile["truncated"] is True
    assert profile["n_latest_available"] == total
    assert profile["profile_top_k"] == 10_000


def test_profile_reflects_version_update_immediately() -> None:
    """版本更新后画像必须立即反映最新值，旧值不得残留。"""
    orchestrator, _ = _make_orchestrator()
    user_id = "version_fresh_user"

    first = orchestrator.store_memory_entry(
        _pref_entry(user_id, key="habit.reply_language", value="中文", content="以后请用中文回答")
    )
    assert first.created is True

    profile_before = orchestrator.get_preference_profile(user_id)
    assert profile_before["kv"]["habit.reply_language"] == "中文"

    second = orchestrator.store_memory_entry(
        _pref_entry(
            user_id,
            key="habit.reply_language",
            value="英文",
            content="以后请用英文回答",
            version=2,
            supersedes=first.memory_id,
        )
    )
    assert second.created is True

    profile_after = orchestrator.get_preference_profile(user_id)
    assert profile_after["kv"]["habit.reply_language"] == "英文", "版本更新后画像必须使用最新值"
    assert profile_after["n_features"] == 1, "旧版本不得作为独立特征残留"
    assert profile_after["truncated"] is False


def test_profile_reflects_forget_immediately() -> None:
    """遗忘后画像与计数必须立即一致（无陈旧画像）。"""
    orchestrator, client = _make_orchestrator()
    user_id = "forget_fresh_user"
    stored = orchestrator.store_memory_entry(
        _pref_entry(
            user_id,
            key="habit.download_path",
            value="D 盘",
            content="以后文件都下载到 D 盘",
        )
    )
    assert stored.created is True
    assert orchestrator.get_preference_profile(user_id)["kv"].get("habit.download_path") == "D 盘"
    before_stats = orchestrator.memory_stats(user_id).milvus.count

    orchestrator.security.forget = lambda uid, instruction: [stored.memory_id]  # type: ignore[method-assign]
    deleted = orchestrator.forget(user_id, "忘掉下载路径的偏好")
    assert stored.memory_id in deleted

    profile_after = orchestrator.get_preference_profile(user_id)
    assert "habit.download_path" not in profile_after["kv"], "遗忘后画像不得残留被删偏好"
    assert orchestrator.memory_stats(user_id).milvus.count == before_stats - 1
    assert client.count_calls, "遗忘后的一致性由 count 聚合保证"


def test_profile_isolated_across_users() -> None:
    """同一存储上多用户画像不得串数据。"""
    orchestrator, _ = _make_orchestrator()
    orchestrator.store_memory_entry(
        _pref_entry("user_a", key="habit.reply_language", value="中文", content="user_a 用中文")
    )
    orchestrator.store_memory_entry(
        _pref_entry("user_b", key="habit.terminal_shell", value="powershell", content="user_b 用 powershell")
    )

    profile_a = orchestrator.get_preference_profile("user_a")
    profile_b = orchestrator.get_preference_profile("user_b")

    assert "habit.reply_language" in profile_a["kv"]
    assert "habit.terminal_shell" not in profile_a["kv"], "user_a 画像不得包含 user_b 的偏好"
    assert "habit.terminal_shell" in profile_b["kv"]
    assert "habit.reply_language" not in profile_b["kv"], "user_b 画像不得包含 user_a 的偏好"


def test_recall_still_returns_full_records_for_ranking() -> None:
    """recall 词面补召依赖 content，投影参数必须保持默认全字段（向后兼容）。"""
    orchestrator, client = _make_orchestrator()
    client.insert(
        memory_id="recall-know-1",
        content="XingHeNotes 默认端口是 18080",
        embedding=[0.0] * 48,
        metadata={"user_id": "recall_user", "memory_type": "project_context", "is_latest": True},
    )

    result = orchestrator.recall(
        RetrievalQuery(query="XingHeNotes 默认端口", user_id="recall_user", top_k=5)
    )
    assert any(item.content for item in result.items), "recall 召回必须保留 content 用于排序"
    assert client.search_calls, "recall 仍应执行检索"
