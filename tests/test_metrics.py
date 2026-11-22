"""Evaluation framework — 硬指标评测基线.

Module: testing | Requirement: (7) 量化评测
Updated: 2026-08-09 移除 skip，用确定性组件构建可重复的评测基线。

硬指标:
  - 偏好提取准确率 >= 85%
  - 知识检索召回率 >= 85%
  - 检索响应时间 <= 500ms
  - 冲突处理正确率 >= 88%

说明：真实跑分依赖 benchmark 数据集 + Milvus/Embedding 真机；此处用
MemoryClassifier / ConflictResolver / MilvusClient(mock) 提供可重复的
组件级评测基线，作为真机评测的替身。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zlib
from pathlib import Path

from src.common.models import MemoryItem, MemoryLayer, MemoryType, NormalizedRecord, RetrievalQuery
from src.core.classifier import MemoryClassifier
from src.core.conflict_resolver import ConflictAction, ConflictResolver
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient

# 硬指标阈值（题目要求）
PREFERENCE_ACCURACY_TARGET = 0.85
RETRIEVAL_RECALL_TARGET = 0.85
LATENCY_BUDGET_MS = 500
CONFLICT_CORRECTNESS_TARGET = 0.88


# ---------------------------------------------------------------------------
# 1. 偏好提取准确率（组件级：MemoryClassifier 分类）
# ---------------------------------------------------------------------------


def _make_normalized(content: str, type_: str, **metadata) -> NormalizedRecord:
    from src.common.models import DataSource

    return NormalizedRecord(
        type=type_,
        content=content,
        user_id="bench",
        source=DataSource.CONVERSATION,
        metadata=metadata,
    )


def _preference_classification_cases() -> list[tuple[str, str, str]]:
    """(content, source_type, expected_type_str)，中文偏好/知识标注样例。"""
    return [
        # 明确偏好
        ("我喜欢用中文回答", "conversation", "preference"),
        ("用户偏好暗色主题界面", "manual_config", "preference"),
        ("她习惯把下载目录放 D 盘", "behavior", "preference"),
        ("不希望自动更新软件", "conversation", "preference"),
        ("我喜欢 Python，不喜欢 Java", "conversation", "preference"),
        ("我讨厌吃榴莲", "conversation", "preference"),
        # 明确知识/事实
        ("Ubuntu 安装 ROS2 的方法", "conversation", "knowledge"),
        ("如何配置国内 pip 镜像", "conversation", "knowledge"),
        ("SSH 免密登录配置步骤", "tool_result", "knowledge"),
        ("什么是向量数据库", "conversation", "knowledge"),
        ("安装依赖失败时换国内源", "tool_result", "knowledge"),
        # 来源偏向：manual_config 偏偏好
        ("theme_dark=true", "manual_config", "preference"),
        # 来源偏向：tool_result 偏知识
        ("exit_code=1", "tool_result", "knowledge"),
    ]


def test_preference_classification_accuracy() -> None:
    """偏好分类准确率目标 >= 85%（MemoryClassifier 组件级口径，与字段级抽取分列）。"""
    classifier = MemoryClassifier()
    cases = _preference_classification_cases()
    correct = 0
    for content, source_type, expected in cases:
        record = _make_normalized(content, source_type)
        actual = classifier.classify(record).value
        if actual == expected:
            correct += 1
    accuracy = correct / len(cases)
    assert accuracy >= PREFERENCE_ACCURACY_TARGET, (
        f"偏好提取准确率 {accuracy:.2%} < {PREFERENCE_ACCURACY_TARGET:.0%}"
    )


def test_preference_classification_reports_accuracy() -> None:
    """记录分类准确率数值，供评测报告引用。"""
    classifier = MemoryClassifier()
    cases = _preference_classification_cases()
    correct = sum(
        1
        for content, source_type, expected in cases
        if classifier.classify(_make_normalized(content, source_type)).value == expected
    )
    print(f"[评测] 偏好提取准确率: {correct}/{len(cases)} = {correct / len(cases):.2%}")


# ---------------------------------------------------------------------------
# 2. 知识检索召回率（组件级：MilvusClient mock 余弦排序）
# ---------------------------------------------------------------------------


def _hash_embedding(text: str) -> list[float]:
    """确定性字符哈希向量：相似文本重叠越多，向量夹角越小。

    注意：必须使用进程间稳定的哈希（zlib.crc32），不能用内置 hash()——
    后者受 PYTHONHASHSEED 影响，每次 pytest 进程结果不同，会让召回率
    变成概率性通过/失败（flaky）。
    """
    # 48 维向量，按字符 bigram 计数哈希投影
    vec = [0.0] * 48
    normalized = text.lower()
    for i in range(len(normalized) - 1):
        pair = normalized[i : i + 2]
        idx = zlib.crc32(pair.encode("utf-8")) % 48
        vec[idx] += 1.0
    return vec


def _build_retrieval_corpus() -> tuple[InMemoryMilvusClient, list[tuple[str, str]]]:
    """插入知识库语料，返回 (client, [(query, relevant_content)])。"""
    client = InMemoryMilvusClient()
    documents = [
        "pip 安装超时时应使用国内镜像源",
        "Ubuntu 系统使用 apt 安装 ROS2 的完整步骤",
        "SSH 免密登录需要将公钥追加到 authorized_keys",
        "Python 虚拟环境使用 conda 创建与激活",
        "Docker 容器端口映射使用 -p 参数",
        "Git 回退到上一版本使用 git reset",
        "麒麟系统开启防火墙需要配置 iptables",
        "Linux 查看磁盘占用使用 df -h",
        "NVIDIA 驱动安装后需要重启才能生效",
        "Kubernetes 部署应用需要编写 deployment.yaml",
    ]
    relevant = [
        ("pip 镜像怎么设置", "pip 安装超时时应使用国内镜像源"),
        ("ROS2 安装教程", "Ubuntu 系统使用 apt 安装 ROS2 的完整步骤"),
        ("SSH 免密配置", "SSH 免密登录需要将公钥追加到 authorized_keys"),
        ("conda 环境怎么创建", "Python 虚拟环境使用 conda 创建与激活"),
        ("Docker 端口映射", "Docker 容器端口映射使用 -p 参数"),
    ]
    for idx, doc in enumerate(documents):
        client.insert(
            memory_id=f"k-{idx}",
            content=doc,
            embedding=_hash_embedding(doc),
            metadata={"user_id": "bench"},
        )
    return client, relevant


def _recall_at_k(hits: list[dict], relevant_content: str) -> bool:
    """top-k 结果中是否命中相关文档。"""
    return any(hit["content"] == relevant_content for hit in hits)


def test_knowledge_retrieval_recall() -> None:
    """知识检索召回率目标 >= 85%（top-5，组件级基线）。"""
    client, relevant_pairs = _build_retrieval_corpus()
    hits = 0
    for query, relevant_content in relevant_pairs:
        results = client.search(
            _hash_embedding(query),
            filters={"user_id": "bench"},
            top_k=5,
        )
        if _recall_at_k(results, relevant_content):
            hits += 1
    recall = hits / len(relevant_pairs)
    assert recall >= RETRIEVAL_RECALL_TARGET, (
        f"检索召回率 {recall:.2%} < {RETRIEVAL_RECALL_TARGET:.0%}"
    )


def test_knowledge_retrieval_reports_recall() -> None:
    client, relevant_pairs = _build_retrieval_corpus()
    hits = sum(
        1
        for query, relevant_content in relevant_pairs
        if _recall_at_k(
            client.search(_hash_embedding(query), filters={"user_id": "bench"}, top_k=5),
            relevant_content,
        )
    )
    print(f"[评测] 检索召回率: {hits}/{len(relevant_pairs)} = {hits / len(relevant_pairs):.2%}")


# ---------------------------------------------------------------------------
# 3. 检索响应时间
# ---------------------------------------------------------------------------


def test_retrieval_latency() -> None:
    """检索响应时间目标 <= 500ms（组件级基线，100 次检索平均）。"""
    client, _ = _build_retrieval_corpus()
    query_vec = _hash_embedding("pip 镜像")
    iterations = 100

    start = time.perf_counter()
    for _ in range(iterations):
        client.search(query_vec, filters={"user_id": "bench"}, top_k=5)
    elapsed_ms = (time.perf_counter() - start) * 1000 / iterations

    assert elapsed_ms <= LATENCY_BUDGET_MS, (
        f"平均检索耗时 {elapsed_ms:.1f}ms > {LATENCY_BUDGET_MS}ms"
    )
    print(f"[评测] 平均检索耗时: {elapsed_ms:.2f}ms")


# ---------------------------------------------------------------------------
# 4. 冲突处理正确率
# ---------------------------------------------------------------------------


def _memory_item(
    content: str,
    *,
    user_id: str = "bench",
    source_type: str = "conversation",
    confidence: float = 0.9,
    quality_score: float | None = None,
    updated_at=None,
) -> MemoryItem:
    kwargs: dict = {}
    if updated_at is not None:
        kwargs["updated_at"] = updated_at
    return MemoryItem(
        id=f"m-{zlib.crc32(content.encode('utf-8')) % 100000}",
        memory_type=MemoryType.PREFERENCE,
        layer=MemoryLayer.LONG,
        content=content,
        user_id=user_id,
        metadata={
            "source_type": source_type,
            "confidence": confidence,
            "preference_key": "reply_language",
        },
        **kwargs,
    )


def _conflict_cases() -> list[tuple[MemoryItem, MemoryItem, ConflictAction]]:
    """(old, new, expected_action)，按 ConflictResolver 规则构造。"""
    from datetime import datetime, timedelta

    cases: list[tuple[MemoryItem, MemoryItem, ConflictAction]] = [
        # 新来源更可信（manual_config > conversation）→ REPLACE
        (
            _memory_item("喜欢中文", source_type="conversation", confidence=0.8),
            _memory_item("喜欢英文", source_type="manual_config", confidence=0.9),
            ConflictAction.REPLACE,
        ),
        # 同来源，新信息时间更近 → REPLACE
        (
            _memory_item(
                "默认语言中文",
                source_type="manual_config",
                updated_at=datetime.utcnow() - timedelta(days=3),
            ),
            _memory_item(
                "默认语言英文",
                source_type="manual_config",
                updated_at=datetime.utcnow(),
            ),
            ConflictAction.REPLACE,
        ),
        # 旧来源更可信（conversation > behavior），内容冲突 → KEEP_OLD
        (
            _memory_item("回复用中文", source_type="conversation", confidence=0.9),
            _memory_item("回复用英文", source_type="behavior", confidence=0.6),
            ConflictAction.KEEP_OLD,
        ),
        # 同信任度同时间 → REPLACE（new_time >= old_time 兜底）
        (
            _memory_item("字体大一点", source_type="conversation"),
            _memory_item("字体小一点", source_type="conversation"),
            ConflictAction.REPLACE,
        ),
        # 内容相同 → 不构成冲突（detect 应返回 False）
        (
            _memory_item("主题暗色", source_type="conversation"),
            _memory_item("主题暗色", source_type="conversation"),
            ConflictAction.KEEP_OLD,  # detect=False 时不走 resolve，视为正确保留
        ),
        # 质量分参与：manual_config(3) + 高质量 → 仍 REPLACE
        (
            _memory_item("下载到 D 盘", source_type="conversation"),
            _memory_item(
                "下载到 E 盘",
                source_type="manual_config",
                quality_score=0.9,
            ),
            ConflictAction.REPLACE,
        ),
    ]
    return cases


def test_conflict_resolution_correctness() -> None:
    """冲突处理正确率目标 >= 88%（组件级基线）。"""
    resolver = ConflictResolver()
    cases = _conflict_cases()
    correct = 0
    for old, new, expected in cases:
        # detect=False（内容一致）视为正确处理
        if not resolver.detect(old, new):
            if expected is ConflictAction.KEEP_OLD:
                correct += 1
            continue
        decision = resolver.resolve(old, new)
        if decision.action == expected:
            correct += 1
    accuracy = correct / len(cases)
    assert accuracy >= CONFLICT_CORRECTNESS_TARGET, (
        f"冲突处理正确率 {accuracy:.2%} < {CONFLICT_CORRECTNESS_TARGET:.0%}"
    )


def test_conflict_resolution_reports_accuracy() -> None:
    resolver = ConflictResolver()
    cases = _conflict_cases()
    correct = 0
    for old, new, expected in cases:
        if not resolver.detect(old, new):
            if expected is ConflictAction.KEEP_OLD:
                correct += 1
            continue
        if resolver.resolve(old, new).action == expected:
            correct += 1
    print(f"[评测] 冲突处理正确率: {correct}/{len(cases)} = {correct / len(cases):.2%}")


# ---------------------------------------------------------------------------
# 5. 跨进程确定性回归（TASK-004 / Issue #3）
#    benchmark 与本文件基线必须同源：向量、记忆 ID、召回在不同
#    PYTHONHASHSEED 与独立进程下完全一致，不允许跑分漂移。
# ---------------------------------------------------------------------------

_CROSS_PROCESS_SAMPLES = ["pip 镜像", "ROS2 安装教程", "SSH 免密配置", "theme_dark=true"]

# 在独立进程中打印 benchmark 向量、crc32 参考向量、记忆 ID 与检索召回。
# JSON 结果逐字节比较，任何 PYTHONHASHSEED 漂移都会让本测试失败。
_CROSS_PROCESS_SNIPPET = """\
import json, sys
sys.path.insert(0, {root})
import benchmarks.run_benchmarks as b
import zlib


def baseline_vec(text, dim=48):
    vec = [0.0] * dim
    normalized = text.lower()
    for i in range(len(normalized) - 1):
        pair = normalized[i : i + 2]
        vec[zlib.crc32(pair.encode("utf-8")) % dim] += 1.0
    return vec


samples = {samples!r}
recall = b._run_retrieval_recall()
print(json.dumps({{
    "vectors": [b._hash_embedding(s) for s in samples],
    "baseline": [baseline_vec(s) for s in samples],
    "ids": [b._memory_item(s).id for s in samples],
    "recall_at_5": recall["recall_at_5"],
    "hit_at_1": recall["hit_at_1"],
}}))
"""


def test_benchmark_hash_embedding_matches_baseline() -> None:
    """benchmark 的向量算法必须与 test_metrics 的 crc32 基线同源。"""
    from benchmarks.run_benchmarks import _hash_embedding as bench_vec

    for text in _CROSS_PROCESS_SAMPLES:
        assert bench_vec(text, 48) == _hash_embedding(text), (
            f"benchmark 向量与基线不一致：{text!r}"
        )


def test_benchmark_metrics_cross_process_deterministic() -> None:
    """固定样本在不同 PYTHONHASHSEED 与独立进程下，向量/ID/召回完全一致。"""
    root = Path(__file__).resolve().parents[1]
    snippet = _CROSS_PROCESS_SNIPPET.format(root=repr(str(root)), samples=_CROSS_PROCESS_SAMPLES)

    env = dict(os.environ)
    results: list[dict] = []
    seeds = ["1", "2", "42"]
    for seed in seeds:
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(root),
            timeout=120,
        )
        assert proc.returncode == 0, f"PYTHONHASHSEED={seed} 子进程失败：{proc.stderr[-500:]}"
        results.append(json.loads(proc.stdout.strip().splitlines()[-1]))

    first = results[0]
    for seed, other in zip(seeds[1:], results[1:]):
        assert other == first, f"PYTHONHASHSEED={seed} 与 seed=1 的结果不一致（跑分漂移）"
    assert first["vectors"] == first["baseline"], "benchmark 向量偏离 crc32 基线"
    assert first["recall_at_5"] >= RETRIEVAL_RECALL_TARGET, (
        f"固定基线召回 {first['recall_at_5']} < {RETRIEVAL_RECALL_TARGET}"
    )
    print(f"[评测] 跨进程一致性: seeds={seeds} 全部一致，Recall@5={first['recall_at_5']}")


# ---------------------------------------------------------------------------
# 6. 评测口径与缺测回归（TASK-005 / Issue #4）
#    baseline 不冒充 real；缺测显式 unavailable 且不计为通过；
#    延迟超标影响总体退出码；分类与字段级抽取分列。
# ---------------------------------------------------------------------------


def test_baseline_report_has_no_real_mode_metrics() -> None:
    """baseline 报告不得包含 real 模式指标；真机状态为 not_requested。"""
    import benchmarks.run_benchmarks as rb

    report = rb._build_report(real=False)
    assert report["mode"] == "baseline"
    assert report["real_backend_status"] == "not_requested"
    assert report["metrics"], "报告必须包含指标条目"
    assert all(m.get("mode") == "baseline" for m in report["metrics"]), (
        "baseline 模式下任何指标都不得标记为 real"
    )
    assert rb._overall_passed(report["metrics"]) is True


def test_real_backend_unavailable_recorded_and_fails_overall(monkeypatch) -> None:
    """原生后端不可达 → 显式 unavailable 缺测条目，不标 real/通过，整体失败。"""
    import benchmarks.run_benchmarks as rb
    from src.knowledge.memory_service import create_memory as create_memory_module
    from src.knowledge.memory_service.milvus_client import MemoryBackendUnavailable

    def boom(request, **kwargs):
        raise MemoryBackendUnavailable("向量后端连接失败：<模拟缺测>")

    monkeypatch.setattr(create_memory_module, "create_memory", boom)
    metric = rb._run_real_retrieval()
    assert metric["mode"] == "real"
    assert metric["status"] == "unavailable"
    assert metric["passed"] is False
    assert metric.get("reason"), "缺测条目必须带脱敏原因"

    report = rb._build_report(real=True)
    assert report["real_backend_status"] == "unavailable"
    assert rb._overall_passed(report["metrics"]) is False, "缺测必须让整体退出码失败"


def test_real_backend_degradation_recorded(monkeypatch) -> None:
    """embedding 降级（stub fallback）必须在真机指标中如实记录。"""
    import benchmarks.run_benchmarks as rb
    from src.embedding import embedding_interface as ei
    from src.knowledge.memory_service import create_memory as create_memory_module
    from src.knowledge.memory_service import embedding_client as embedding_client_module
    from src.knowledge.memory_service import query_memory as query_memory_module
    from src.knowledge.memory_service.memory_schema import (
        CreateMemoryResponse,
        QueryMemoryResponse,
    )

    class _StubProvider:
        _fallback_mode = True
        _model_name = "stub-hash"
        dimension = 48

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * self.dimension for _ in texts]

    stub = _StubProvider()
    # EmbeddingClient 在模块命名空间绑定了 get_shared_embedding_backend，
    # _real_backend_info 则从 embedding_interface 动态导入，两处都要替换。
    monkeypatch.setattr(embedding_client_module, "get_shared_embedding_backend", lambda backend=None: stub)
    monkeypatch.setattr(ei, "get_shared_embedding_backend", lambda backend=None: stub)
    monkeypatch.setattr(
        create_memory_module,
        "create_memory",
        lambda request, **kwargs: CreateMemoryResponse(memory_id="m", created=True),
    )
    monkeypatch.setattr(
        query_memory_module,
        "query_memory",
        lambda request, **kwargs: QueryMemoryResponse(items=[], latency_ms=1.0),
    )

    metric = rb._run_real_retrieval()
    assert metric["status"] == "ran", f"降级不是缺测：{metric.get('reason')}"
    assert metric["mode"] == "real"
    assert metric["fallback"] is True, "降级为确定性向量时必须记录 fallback"
    assert metric["embedding"] == "stub-hash"
    assert metric["embedding_dim"] == 48
    assert metric["passed"] is False  # 空结果召回为 0，如实判失败


def test_recall_pass_latency_fail_fails_overall() -> None:
    """召回过线但延迟超标 → 总体失败（main 退出码口径，Issue #4）。"""
    import benchmarks.run_benchmarks as rb

    recall_pass_latency_fail = {
        "name": "知识检索召回率",
        "mode": "baseline",
        "status": "ran",
        "passed": True,
        "latency_passed": False,
    }
    latency_ok = {**recall_pass_latency_fail, "latency_passed": True}
    all_pass = {**latency_ok}
    assert rb._overall_passed([recall_pass_latency_fail]) is False
    assert rb._overall_passed([latency_ok]) is True
    assert rb._overall_passed([{**all_pass, "passed": False}]) is False
    assert rb._overall_passed([{**all_pass, "status": "unavailable"}]) is False


def test_classification_and_extraction_metrics_are_separate() -> None:
    """分类准确率与字段级抽取分列；抽取接入既有评测入口且金标不变。"""
    import benchmarks.run_benchmarks as rb

    classification = rb._run_preference_classification()
    assert classification["name"] == "偏好分类准确率", "分类口径不得再叫『偏好提取准确率』"
    assert classification["status"] == "ran"
    assert "MemoryClassifier" in classification["backend"]

    extraction = rb._run_preference_extraction()
    assert extraction["name"] == "偏好抽取（字段级）"
    assert extraction["status"] == "ran"
    assert extraction["mode"] == "baseline"
    for field in ("precision", "recall", "f1", "sample_exact_match_rate", "sample_size"):
        assert field in extraction, f"缺少字段级口径字段：{field}"
    assert extraction["passed"] == (extraction["f1"] >= PREFERENCE_ACCURACY_TARGET)
    assert extraction["passed"] is True, (
        f"rule 抽取在金标数据集上 F1={extraction['f1']} 未过线（不改金标，需修抽取）"
    )
    assert extraction["name"] != classification["name"]
