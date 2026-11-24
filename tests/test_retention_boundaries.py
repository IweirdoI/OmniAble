"""TASK-034 / Issue #42 保留边界回归：两用户隔离、遗忘传播、路径可迁移、
流程/插件数据默认不进长期记忆。

对应 docs/DATA_RETENTION.md 的契约；策略事实来源 src/security/retention.py。
"""

from __future__ import annotations

import tempfile

import pytest

from src.common.models import RetrievalQuery
from src.security import retention as rt


# ---------------------------------------------------------------------------
# 契约本身：策略表与遗忘传播清单
# ---------------------------------------------------------------------------

def test_plugin_data_kinds_are_never_long_term_ingestible() -> None:
    """录音/模板/表格行/任务日志/代码索引默认不可摄入长期记忆（验收原文）。"""
    for kind in sorted(rt.PLUGIN_DATA_KINDS):
        assert rt.long_term_ingestible(kind) is False, kind
        policy = rt.get_policy(kind)
        assert policy is not None and policy.long_term_ingestible is False


def test_task_log_policy_keeps_minimal_result_only() -> None:
    """任务日志契约：只留最小结果（不含正文），不承诺遗忘传播。"""
    policy = rt.get_policy("task_log")
    assert policy is not None
    assert "最小结果" in policy.retention
    assert policy.long_term_ingestible is False


def test_template_and_authorization_are_separate_kinds() -> None:
    """模板与执行授权显式分开（交付要求），模板不含授权。"""
    template = rt.get_policy("template")
    authz = rt.get_policy("execution_authorization")
    assert template is not None and authz is not None
    assert template.kind != authz.kind
    assert "独立于模板" in authz.retention


def test_forget_propagation_targets_cover_all_user_stores() -> None:
    """遗忘传播清单覆盖全部按用户落盘的存储（新增存储必须登记）。"""
    assert set(rt.FORGET_PROPAGATION_TARGETS) == {
        "sensitive_index", "short_term_layer", "mid_term_layer", "long_term_layer",
        "vector_backend", "ingestion_staging", "preference_version_index",
        "agent_session_references",
    }


def test_policy_kinds_have_unique_stable_ids() -> None:
    """类别标识稳定且唯一，位置字段与契约文档一致（data/ 前缀）。"""
    kinds = [p.kind for p in rt.RETENTION_POLICY]
    assert len(kinds) == len(set(kinds))
    for policy in rt.RETENTION_POLICY:
        if policy.location and policy.location.startswith("<"):
            continue
        if policy.location:
            assert not policy.location[1:2] == ":", "位置不得内嵌盘符"


# ---------------------------------------------------------------------------
# 运行时回归：两用户隔离 / 遗忘传播 / 路径迁移
# ---------------------------------------------------------------------------

def _build_orchestrators(storage_dir: str, *, milvus=None):
    """按 test_security 验收测试同款方式构造 A/B 两个独立编排器。

    milvus 不传时各自使用独立内存后端；传入共享实例用于模拟持久后端
    （重启/换盘场景下向量数据保留）。
    """
    from src.core.orchestrator import MemoryOrchestrator
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
    from src.preference.store import PreferenceMemoryStore

    pref_a = PreferenceMemoryStore(storage_dir=storage_dir, use_llm=False, allow_rules=True)
    pref_b = PreferenceMemoryStore(storage_dir=storage_dir, use_llm=False, allow_rules=True)
    orch_a = MemoryOrchestrator(preference=pref_a, milvus=milvus or InMemoryMilvusClient())
    orch_b = MemoryOrchestrator(preference=pref_b, milvus=milvus or InMemoryMilvusClient())
    return orch_a, orch_b


def test_two_users_isolated_across_sessions_and_recall(tmp_path) -> None:
    """两名虚构用户：偏好画像、画像读取与向量召回互不串数据。"""
    storage_dir = str(tmp_path / "prefs")
    orch_a, orch_b = _build_orchestrators(storage_dir)

    orch_a.extract_preferences_from_text(
        "下载文件默认保存到 C:/data/downloads", user_id="u_a", scene_id="general")
    orch_b.extract_preferences_from_text(
        "下载文件默认保存到 E:/data/english_dir", user_id="u_b", scene_id="general")

    profile_a = orch_a.get_preference_profile("u_a", scene_id="general")
    profile_b = orch_b.get_preference_profile("u_b", scene_id="general")
    text_a = str(profile_a)
    text_b = str(profile_b)
    assert "C:/data/downloads" in text_a and "E:/data/english_dir" not in text_a
    assert "E:/data/english_dir" in text_b and "C:/data/downloads" not in text_b

    # 向量召回：各自入库的知识只被本人召回（store_memory_entry 长期入库）
    from src.knowledge.memory_service.memory_schema import MemoryEntry

    orch_a.store_memory_entry(MemoryEntry(
        content="用户A的打印机IP是192.168.1.5", memory_type="project_context",
        source="manual", user_id="u_a"))
    orch_b.store_memory_entry(MemoryEntry(
        content="用户B的代码仓库在 D:/repo_b", memory_type="project_context",
        source="manual", user_id="u_b"))
    hits_a = orch_a.recall(RetrievalQuery(query="打印机", user_id="u_a", top_k=5)).items
    hits_b = orch_b.recall(RetrievalQuery(query="打印机", user_id="u_b", top_k=5)).items
    # 注意：入库先经脱敏，IP 被替换为 <HASH_xxx> 占位符——断言可召回的
    # 内容主体而非原始值；u_b 不得看到 u_a 的任何记录。
    assert any("用户A的打印机IP" in item.content for item in hits_a)
    assert not any("repo_b" in item.content for item in hits_a)
    assert not hits_b


def test_forget_propagates_and_survives_restart(tmp_path) -> None:
    """遗忘后：向量后端（重启后新实例）、画像与召回均不重现应删内容。"""
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    storage_dir = str(tmp_path / "prefs")
    # 共享持久向量后端（模拟 milvus-lite / ECS 持久盘）：重启后数据仍在
    persistent_milvus = InMemoryMilvusClient()
    orch_a, _ = _build_orchestrators(storage_dir, milvus=persistent_milvus)

    orch_a.process_input({"content": "我的手机号是13800000001，临时记一下", "role": "user"},
                         "conversation", user_id="u_a")
    res = orch_a.extract_preferences_from_text(
        "下载文件默认保存到 E:/data/downloads", user_id="u_a", scene_id="general")
    assert res.entries, "前置：偏好提取应产出条目"
    deleted = orch_a.forget("u_a", "忘掉下载路径偏好")
    assert deleted, "遗忘应删除至少一条记忆"

    # 重启：同一持久后端 + 同一数据目录重建实例（模拟服务重启恢复）
    orch_restarted, _ = _build_orchestrators(storage_dir, milvus=persistent_milvus)
    profile = orch_restarted.get_preference_profile("u_a", scene_id="general")
    assert "E:/data/downloads" not in str(profile)

    for query in ("手机号", "13800000001", "下载路径"):
        hits = orch_restarted.recall(
            RetrievalQuery(query=query, user_id="u_a", top_k=10)).items
        assert not any("13800000001" in item.content or "E:/data/downloads" in item.content
                       for item in hits), f"遗忘内容经重启后仍可召回：{query}"


def test_storage_layout_survives_relocation(tmp_path) -> None:
    """换盘/迁移：整个数据目录挪到新根后，两用户隔离与画像读取依旧成立。"""
    import shutil
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    storage_dir = str(tmp_path / "prefs")
    persistent_milvus = InMemoryMilvusClient()
    orch_a, orch_b = _build_orchestrators(storage_dir, milvus=persistent_milvus)
    orch_a.extract_preferences_from_text(
        "下载文件默认保存到 C:/data/downloads", user_id="u_a", scene_id="general")
    orch_b.extract_preferences_from_text(
        "下载文件默认保存到 D:/data/dl_dir", user_id="u_b", scene_id="general")

    # 模拟换盘：原目录整体复制到新根（盘符无关的相对布局），向量后端数据保留
    new_root = tmp_path / "moved" / "different_drive"
    new_root.mkdir(parents=True)
    shutil.copytree(storage_dir, str(new_root / "prefs"))
    orch_a_moved, orch_b_moved = _build_orchestrators(
        str(new_root / "prefs"), milvus=persistent_milvus)

    assert "D:/data/dl_dir" in str(orch_b_moved.get_preference_profile("u_b", scene_id="general"))
    assert "D:/data/dl_dir" not in str(orch_a_moved.get_preference_profile("u_a", scene_id="general"))
    assert "C:/data/downloads" not in str(orch_b_moved.get_preference_profile("u_b", scene_id="general"))


def test_plugin_sources_never_reach_long_term_memory(tmp_path, monkeypatch) -> None:
    """录音/表格行/源码/模板/任务日志以未登记 source 摄入 → 执行前拒绝。"""
    from src.core.orchestrator import MemoryOrchestrator
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

    monkeypatch.setenv("OS_AGENT_MEMORY_BACKEND", "memory")
    orch = MemoryOrchestrator(preference=None, milvus=InMemoryMilvusClient())
    payload = {"content": "录音转写正文（不应入库）", "role": "user"}
    for source in ("recording", "table_row", "source_code", "template", "task_log"):
        result = orch.process_input(dict(payload), source, user_id="u_a")
        assert result.accepted is False, f"{source} 不应被摄入长期记忆"
        assert result.reject_code is not None

    # 兜底：即便绕过来源校验，向量后端里也查不到该正文
    hits = orch.recall(RetrievalQuery(query="录音转写正文", user_id="u_a", top_k=10)).items
    assert not any("不应入库" in item.content for item in hits)


def test_session_references_fail_closed_across_users(tmp_path) -> None:
    """会话引用按用户失效：B 用 A 的 session_id 读不到任何内容。"""
    from src.agent.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.db")
    try:
        session = store.create_session("u_a", title="A 的会话")
        store.record_completed_turn(
            "u_a", session["id"], "A 的问题", {
                "answer": "A 的回答", "memory_write": {"status": "accepted"},
                "referenced_memory_ids": ["mem-1"],
            }, history_memory_ids={"mem-1"})
        assert store.get_session("u_b", session["id"]) is None
        assert store.get_session("u_a", session["id"]) is not None

        # 遗忘联动后：引用的记忆进墓碑，B 无法借道读取
        store.forget_memories("u_a", ["mem-1"])
        assert store.forgotten_ids("u_a", ["mem-1"]) == {"mem-1"}
        history, source_ids = store.load_model_history("u_a", session["id"])
        assert source_ids == set(), "遗忘后模型历史不得再携带已遗忘记忆来源"
    finally:
        store.close()
