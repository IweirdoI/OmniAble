"""TASK-038 召回优化回归：向量打分等价、词面打分等价、缓存正确性。

对应 docs/TASK-038_recall_optimization.md 的三项优化：
  1. InMemoryMilvusClient 向量化打分（与逐对实现等价）
  2. term_overlap 查询侧预编译（与旧实现逐条等价）
  3. LocalEmbeddingAdapter 查询向量缓存（同文本同向量、跨模型隔离）
"""

from __future__ import annotations

import math
import random

import pytest

from src.core import recall_ranker as rr
from src.knowledge.memory_service import in_memory_client as imc
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient


def _legacy_term_overlap(query: str, content: str) -> int:
    """TASK-038 之前的原始实现（逐条重复计算查询侧），用于等价性对照。"""
    body = (content or "").lower()
    if not body:
        return 0
    score = 0
    remainder = query.lower()
    active = [g for g in rr._TOPICS if any(rr._contains(remainder, w) for w in g)]
    specific = [g for g in active if g not in rr._TOPICS[-2:]]
    if specific and not any(any(rr._contains(body, w) for w in g) for g in specific):
        return 0
    for group in active:
        if any(rr._contains(body, w) for w in group):
            score += rr._TOPIC_HIT_WEIGHT
        for word in sorted(group, key=len, reverse=True):
            remainder = remainder.replace(word, " ")
    covered: list[str] = []
    for term in sorted(rr.distinctive_terms(remainder), key=len, reverse=True):
        if any(term.lower() in old for old in covered):
            continue
        for needle in rr._overlap_needles(term):
            if rr._contains(body, needle):
                score += (
                    rr._TOKEN_HIT_WEIGHT_LONG
                    if len(needle) >= 4 or needle.isascii()
                    else rr._TOKEN_HIT_WEIGHT_SHORT
                )
                covered.append(term.lower())
                break
    return score


@pytest.mark.parametrize("query,content", [
    ("pip 镜像怎么设置", "pip 安装超时时应使用国内镜像源"),
    ("ROS2 安装教程", "Ubuntu 系统使用 apt 安装 ROS2 的完整步骤"),
    ("SSH 免密配置", "SSH 免密登录需要将公钥追加到 authorized_keys"),
    ("conda 环境怎么创建", "Python 虚拟环境使用 conda 创建与激活"),
    ("Docker 端口映射", "Docker 容器端口映射使用 -p 参数"),
    ("打印机怎么连", "打印机 IP 是 192.168.1.5，端口 9100"),
    ("XingHeNotes default port", "The default port for XingHeNotes is 8080"),
    ("下载路径偏好", "习惯把下载目录放 D 盘"),
    ("", "任意正文"),
    ("任意查询", ""),
])
def test_prepared_overlap_matches_legacy(query: str, content: str) -> None:
    """词面打分预编译实现与旧实现逐条等价。"""
    assert rr.term_overlap(query, content) == _legacy_term_overlap(query, content)


def test_prepared_overlap_matches_legacy_fuzz() -> None:
    """模糊随机组合下与旧实现等价（防止边界遗漏）。"""
    rng = random.Random(20261123)
    alphabet = "pip安装超时镜像源中文回答偏好下载路径打印机端口配置kubernetes docker ssh"
    for _ in range(3000):
        query = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 18)))
        content = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 36)))
        assert rr.term_overlap(query, content) == _legacy_term_overlap(query, content), (
            f"mismatch q={query!r} c={content!r}"
        )


def test_inmemory_vector_scores_match_exact(monkeypatch) -> None:
    """向量化打分与逐对纯 Python 实现：top_k 顺序一致、分数差在浮点噪声级。"""
    rng = random.Random(7)
    dim, n = 64, 200
    vectors = [[rng.uniform(-1, 1) for _ in range(dim)] for _ in range(n)]
    query = [rng.uniform(-1, 1) for _ in range(dim)]

    client = InMemoryMilvusClient()
    for index, vector in enumerate(vectors):
        client.insert(memory_id=f"m-{index:04d}", content=f"doc {index}",
                      embedding=vector, metadata={"user_id": "u"})

    fast = client.search(query, {"user_id": "u"}, top_k=10)
    monkeypatch.setattr(imc, "_EXACT_COSINE", True)
    exact = client.search(query, {"user_id": "u"}, top_k=10)

    assert [r["memory_id"] for r in fast] == [r["memory_id"] for r in exact]
    for a, b in zip(fast, exact, strict=True):
        assert abs(a["score"] - b["score"]) < 1e-12


def test_inmemory_matrix_cache_invalidated_on_write() -> None:
    """插入/更新/硬删除后矩阵缓存失效，新向量立即可被检索到。"""
    client = InMemoryMilvusClient()
    client.insert(memory_id="a", content="alpha", embedding=[1.0, 0.0],
                  metadata={"user_id": "u"})
    assert client.search([1.0, 0.0], {"user_id": "u"}, top_k=5)[0]["memory_id"] == "a"

    client.insert(memory_id="b", content="beta", embedding=[1.0, 0.0],
                  metadata={"user_id": "u"})
    hits = [row["memory_id"] for row in client.search([1.0, 0.0], {"user_id": "u"}, top_k=5)]
    assert set(hits) == {"a", "b"}, "新增记录必须进入检索结果（矩阵缓存已失效）"

    client.delete("a", soft=False)
    hits = [row["memory_id"] for row in client.search([1.0, 0.0], {"user_id": "u"}, top_k=5)]
    assert hits == ["b"], "硬删除后不得再命中"


def test_inmemory_matrix_handles_aliases_bulk_forget_and_clear(monkeypatch) -> None:
    pytest.importorskip("numpy")
    monkeypatch.setattr(imc, "_EXACT_COSINE", False)
    client = InMemoryMilvusClient()
    client.insert("a", "alpha", [1.0, 0.0], {"user_id": "u"})
    client.insert("other", "beta", [1.0, 0.0], {"user_id": "other"})
    client.search([1.0, 0.0], {"user_id": "u"})

    client.get_by_id("a", include_embedding=True)["embedding"][0] = 0.0
    assert client.search([1.0, 0.0], {"user_id": "u"})[0]["score"] == 1.0
    client.update("a", {"embedding": [0.0, 1.0]})
    assert client.search([0.0, 1.0], {"user_id": "u"})[0]["score"] == 1.0

    assert client.delete_by_filters({"user_id": "u"}) == ["a"]
    assert client.search([1.0, 0.0], {"user_id": "u"}) == []
    assert client.search([1.0, 0.0], {"user_id": "other"})[0]["memory_id"] == "other"
    assert client._matrix_ids == ["other"]
    client.clear()
    assert client._matrix is None and client._norms == {}
    client.insert("a", "new alpha", [0.0, 1.0], {"user_id": "u"})
    assert client.search([0.0, 1.0], {"user_id": "u"})[0]["score"] == 1.0


def test_embedding_cache_reuse_and_isolation(monkeypatch) -> None:
    """查询向量缓存：同文本复用同向量、不同文本不同、批量与单条一致。"""
    from src.embedding.local_embedding import LocalEmbeddingAdapter

    calls = {"n": 0}

    class _FakeVec(list):
        def tolist(self):
            return list(self)

    class _FakeModel:
        def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
            calls["n"] += len(texts)
            return [_FakeVec([float(len(t) % 7), float(sum(map(ord, t)) % 11)])
                    for t in texts]

    adapter = LocalEmbeddingAdapter(dimension=2)
    monkeypatch.setattr(adapter, "_ensure_model", lambda: _FakeModel())

    first = adapter.embed_queries(["查询A"])
    second = adapter.embed_queries(["查询A"])
    assert first == second
    assert calls["n"] == 1, "重复查询应命中缓存，不再调用模型"

    batch = adapter.embed_queries(["查询A", "查询B"])
    assert batch[0] == first[0]
    assert calls["n"] == 2, "批量中仅未命中项需要编码"

    other = adapter.embed_queries(["查询B"])
    assert other[0] == batch[1]
    assert adapter.cache_stats()["entries"] == 2
    batch[1][0] = -100
    other[0][0] = -101
    fresh = adapter.embed_queries(["查询B"])
    assert fresh[0][0] not in (-100, -101)
    assert calls["n"] == 2
    assert all(isinstance(key, bytes) for key in adapter._cache)
    adapter.embed(["文档正文"])
    assert adapter.cache_stats()["entries"] == 2


def test_embedding_cache_disabled(monkeypatch) -> None:
    """OS_AGENT_EMBEDDING_CACHE_SIZE=0 时每次都编码（对照口径）。"""
    from src.embedding import local_embedding as le

    calls = {"n": 0}

    class _FakeVec(list):
        def tolist(self):
            return list(self)

    class _FakeModel:
        def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
            calls["n"] += len(texts)
            return [_FakeVec([1.0]) for _ in texts]

    monkeypatch.setattr(le, "_CACHE_SIZE", 0)
    adapter = le.LocalEmbeddingAdapter(dimension=1)
    monkeypatch.setattr(adapter, "_ensure_model", lambda: _FakeModel())
    adapter.embed_queries(["同一个查询"])
    adapter.embed_queries(["同一个查询"])
    assert calls["n"] == 2
