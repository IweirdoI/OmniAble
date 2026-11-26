"""Local embedding for dev/test (BGE-M3). Module: core."""

from __future__ import annotations

import logging
import hashlib
import math
import os
import re
import threading
from collections import OrderedDict
from typing import Any

from src.common.interfaces import EmbeddingProvider

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-m3"
_MODEL_CACHE: dict[tuple[str, str | None], Any] = {}
_MODEL_LOCK = threading.Lock()

# 查询向量缓存与编码并发策略（TASK-038 / Issue #47）
# 实测（BGE-M3 CPU，Windows，Windows 10 / Zen3）：
#   1 串行编码 P50 116ms；4 并发 P50 378ms（3.2x）、P95 688ms，吞吐仅 8.15→9.15 ops/s（+12%）
#   → CPU 争抢为主，外部并发并不能提高吞吐。
#   尝试串行化模型调用后：P50 恢复到 117ms，但队尾 P95 恶化到 1488ms、吞吐降到 7.73
#   → 排队把延迟集中到队尾，对「P95 ≤ 500ms」目标反而更差。
# 结论（按实测选择）：**不串行化**（保留开关供对照），采用**有界 LRU 查询向量缓存**——
# 重复查询直接命中（0ms），这是该环境下唯一不牺牲 P95/吞吐的收益点。
# 查询键使用实例独立的带密钥摘要。摄取与更新正文不使用查询缓存。
# 模型名、维度与归一化参数由实例确定。
# 容量由 OS_AGENT_EMBEDDING_CACHE_SIZE 控制（0 关闭，默认 1024，进程内 LRU）。
_CACHE_SIZE = int(os.getenv("OS_AGENT_EMBEDDING_CACHE_SIZE", "1024"))
_SERIALIZE_ENCODE = os.getenv("OS_AGENT_EMBEDDING_SERIALIZE", "0").strip().lower() in {
    "1", "true", "yes", "on",
}


class LocalEmbeddingAdapter(EmbeddingProvider):
    """Windows / 开发机 fallback：sentence-transformers BGE-M3."""

    def __init__(
        self,
        *,
        model_name: str = _DEFAULT_MODEL,
        device: str | None = None,
        normalize: bool = True,
        dimension: int | None = None,
        allow_stub_fallback: bool = True,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._normalize = normalize
        self._dimension = dimension or 1024
        self._allow_stub_fallback = allow_stub_fallback
        self._fallback_mode = False
        self._model: Any | None = None
        # Keep request text out of the cache, and never cache ingestion content.
        self._cache_key = os.urandom(32)
        self._cache: OrderedDict[bytes, tuple[float, ...]] = OrderedDict()
        self._cache_lock = threading.Lock()
        # 编码串行化：避免多线程并发调用同一模型导致 CPU 争抢与 P95 抖动
        self._encode_gate = threading.Semaphore(1)

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        cache_key = (self._model_name, self._device)
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(cache_key)
            if cached is not None:
                self._model = cached
                return self._model

            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                if self._allow_stub_fallback:
                    logger.warning(
                        "sentence-transformers 未安装，local embedding 回退为确定性哈希向量。"
                        "Windows 开发请执行: pip install sentence-transformers"
                    )
                    self._fallback_mode = True
                    return None
                raise ImportError(
                    "本地 embedding 需要 sentence-transformers。"
                    "请执行: pip install sentence-transformers"
                ) from exc

            logger.info("Loading local embedding model: %s", self._model_name)
            kwargs: dict[str, Any] = {}
            if self._device:
                kwargs["device"] = self._device
            # 离线档：禁止向 HF 等远端仓库发起模型下载（仅用本地缓存；
            # 无缓存时按既有 fallback 链降级为哈希向量并明确警告）
            from src.common.offline_profile import offline_mode
            if offline_mode():
                kwargs["local_files_only"] = True
            try:
                model = SentenceTransformer(self._model_name, **kwargs)
            except Exception:
                if not self._allow_stub_fallback:
                    raise
                logger.exception(
                    "本地 embedding 模型加载失败，回退为确定性哈希向量；"
                    "该模式仅供离线联调，不可用于正式召回指标。"
                )
                self._fallback_mode = True
                return None
            _MODEL_CACHE[cache_key] = model
            self._model = model
            if self._dimension is None:
                self._dimension = int(model.get_sentence_embedding_dimension())
            return model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        if model is None:
            return [self._hash_embed(text) for text in texts]
        return self._encode(model, texts)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if _CACHE_SIZE <= 0:
            return self.embed(texts)
        if not texts:
            return []

        model = self._ensure_model()
        if model is None:
            return [self._hash_embed(text) for text in texts]

        cache_keys = [hashlib.blake2b(text.encode("utf-8", errors="surrogatepass"),
                                    key=self._cache_key, digest_size=32).digest()
                      for text in texts]
        results: list[list[float]] = [[] for _ in texts]
        missing: list[int] = []
        with self._cache_lock:
            for index, key in enumerate(cache_keys):
                cached = self._cache.get(key)
                if cached is None:
                    missing.append(index)
                else:
                    self._cache.move_to_end(key)
                    results[index] = list(cached)

        if missing:
            payload = [texts[index] for index in missing]
            vectors = self._encode(model, payload)
            with self._cache_lock:
                for index, vector in zip(missing, vectors, strict=True):
                    results[index] = vector
                    key = cache_keys[index]
                    self._cache[key] = tuple(vector)
                    self._cache.move_to_end(key)
                while len(self._cache) > _CACHE_SIZE:
                    self._cache.popitem(last=False)
        return results

    def _encode(self, model: Any, texts: list[str]) -> list[list[float]]:
        if _SERIALIZE_ENCODE:
            with self._encode_gate:
                return self._encode_batch(model, texts)
        return self._encode_batch(model, texts)

    def _encode_batch(self, model: Any, texts: list[str]) -> list[list[float]]:
        vectors = model.encode(texts, normalize_embeddings=self._normalize, show_progress_bar=False)
        if self._dimension is None:
            self._dimension = len(vectors[0])
        return [vector.tolist() for vector in vectors]

    def cache_stats(self) -> dict[str, int]:
        """查询向量缓存规模（观测用；不影响行为）。"""
        with self._cache_lock:
            return {"entries": len(self._cache), "capacity": _CACHE_SIZE}

    @property
    def dimension(self) -> int:
        if not self._fallback_mode and self._dimension is None:
            self._ensure_model()
        return int(self._dimension)

    def _hash_embed(self, text: str) -> list[float]:
        """无模型时保留词/字粒度重叠信号，避免零向量伪装成功。"""

        vector = [0.0] * self._dimension
        normalized = " ".join(text.lower().split())
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)
        tokens.extend(normalized[index : index + 2] for index in range(max(0, len(normalized) - 1)))
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self._dimension
            vector[index] += 1.0
        if self._normalize:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
        return vector


class HashEmbeddingAdapter(LocalEmbeddingAdapter):
    """Fast deterministic backend for unit tests and offline wiring checks."""

    def __init__(self, *, dimension: int = 1024) -> None:
        super().__init__(dimension=dimension, allow_stub_fallback=True)
        self._fallback_mode = True

    def _ensure_model(self) -> None:
        return None
