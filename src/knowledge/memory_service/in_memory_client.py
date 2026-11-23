"""Deterministic in-memory memory-service backend for tests and local demos.

This backend is deliberately separate from the real pymilvus wrapper so test
results cannot be mistaken for Milvus deployment or performance validation.
"""

from __future__ import annotations

import math
import os
import threading
from typing import Any

try:  # numpy 由 pandas 依赖带来；不可用时回退纯 Python（保持兼容）
    import numpy as _np
except ImportError:  # pragma: no cover - 环境缺少 numpy 时的兜底
    _np = None

# 对照开关：置 1 时强制使用逐对纯 Python 余弦（用于精度对照与回归定位）
_EXACT_COSINE = os.getenv("OS_AGENT_INMEMORY_EXACT_COSINE", "").strip().lower() in {
    "1", "true", "yes", "on",
}


def _norm(vector: list[float]) -> float:
    if not vector:
        return 0.0
    return math.sqrt(sum(value * value for value in vector))


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class InMemoryMilvusClient:
    """Milvus-compatible process-local backend without persistence."""

    def __init__(
        self,
        *_args: Any,
        uri: str = "memory://",
        collection: str = "os_agent_memory",
        **_kwargs: Any,
    ) -> None:
        self.uri = uri
        self.collection = collection
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        # 存储向量范数缓存：避免每次检索重复计算（TASK-038）
        self._norms: dict[str, float] = {}
        # 向量矩阵缓存：避免每次检索从 list-of-list 重建 N×dim 数组（TASK-038）
        self._matrix: Any = None
        self._matrix_ids: list[str] = []
        self._matrix_index: dict[str, int] = {}

    def _invalidate_matrix(self) -> None:
        self._matrix = None
        self._matrix_ids = []
        self._matrix_index = {}

    def insert(
        self,
        memory_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> bool:
        record = {
            "memory_id": memory_id,
            "content": content,
            "embedding": list(embedding),
            "is_deleted": False,
            **metadata,
        }
        if "embedding" in metadata and isinstance(record["embedding"], list):
            record["embedding"] = list(record["embedding"])
        with self._lock:
            self._records[memory_id] = record
            self._norms[memory_id] = _norm(record["embedding"]) if isinstance(record["embedding"], list) else 0.0
            self._invalidate_matrix()
        return True

    @staticmethod
    def _matches_filters(record: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, value in filters.items():
            actual = record.get(key)
            if isinstance(value, (list, tuple, set)):
                if actual not in value:
                    return False
            elif actual != value:
                return False
        return True

    def _score_batch(
        self,
        query: list[float],
        matches: list[tuple[str, dict[str, Any]]],
        top_k: int,
    ) -> list[float]:
        """对已过滤的候选批量打分（TASK-038）。

        向量化路径：一次矩阵乘 + 预算范数（插入时缓存），替代每条重复计算
        查询范数与存储范数的逐对纯 Python 循环；结果与逐对实现等价（浮点
        累加顺序差异在 1e-15 量级，不改变金标排序）。numpy 不可用、维度不一致
        或显式要求精确对照（OS_AGENT_INMEMORY_EXACT_COSINE=1）时回退逐对实现。
        """
        def exact(record: dict[str, Any]) -> float:
            vector = record.get("embedding")
            return _cosine_similarity(query, vector) if isinstance(vector, list) else 0.0

        if _np is None or _EXACT_COSINE or top_k <= 0:
            return [exact(record) for _, record in matches]
        try:
            query_vec = _np.asarray(query, dtype=_np.float64)
            query_norm = float(_np.linalg.norm(query_vec))
            self._ensure_matrix()
            matrix = self._matrix
            if matrix is None or matrix.shape[1] != query_vec.shape[0]:
                raise ValueError("dimension mismatch")
            norms = _np.asarray(
                [self._norms.get(memory_id, 0.0) for memory_id, _ in matches],
                dtype=_np.float64,
            )
            if len(matches) == len(self._matrix_ids):
                # 全量命中：直接整矩阵相乘，零拷贝
                dots = matrix @ query_vec
            else:
                rows = _np.asarray(
                    [self._matrix_index[memory_id] for memory_id, _ in matches],
                    dtype=_np.intp,
                )
                dots = matrix[rows] @ query_vec
            denominator = norms * query_norm
            if not _np.isfinite(denominator).all() or not _np.isfinite(dots).all():
                return [exact(record) for _, record in matches]
            scores = _np.zeros(len(matches), dtype=_np.float64)
            valid = denominator > 0.0
            if valid.any():
                scores[valid] = dots[valid] / denominator[valid]
            # Re-score the result set and any close boundary ties so the public
            # scores and stable ordering use the original accumulation order.
            boundary = max(0, len(scores) - top_k)
            cutoff = float(_np.partition(scores, boundary)[boundary])
            for index in _np.flatnonzero(scores >= cutoff - 1e-12):
                scores[index] = exact(matches[index][1])
            return scores.tolist()
        except Exception:  # noqa: BLE001 - 任何异常回退逐对实现，保持正确性
            return [exact(record) for _, record in matches]

    def _ensure_matrix(self) -> None:
        """按插入顺序构建/复用存储向量矩阵（脏标记驱动，TASK-038）。"""
        if self._matrix is not None:
            return
        ids = [memory_id for memory_id, record in self._records.items()
               if not record.get("is_deleted")]
        self._matrix_ids = ids
        self._matrix_index = {memory_id: index for index, memory_id in enumerate(ids)}
        if _np is None or not ids:
            self._matrix = None
        else:
            try:
                self._matrix = _np.asarray(
                    [self._records[memory_id]["embedding"] for memory_id in ids],
                    dtype=_np.float64,
                )
            except Exception:  # noqa: BLE001 - 维度不一致等异常时退化为逐对实现
                self._matrix = None

    def search(
        self,
        embedding: list[float] | None,
        filters: dict[str, Any],
        top_k: int = 5,
        *,
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """字段投影：output_fields=None 时保持全字段（向后兼容）。

        TASK-006：画像路径只需元数据字段，投影可避免把大 content 复制给调用方。
        TASK-038：打分改为批量向量化（见 _score_batch），过滤与排序语义不变。
        """
        with self._lock:
            matches = [(memory_id, record) for memory_id, record in self._records.items()
                       if not record.get("is_deleted") and self._matches_filters(record, filters)]
            if not matches:
                return []
            scores = (self._score_batch(embedding, matches, top_k) if embedding else [0.0] * len(matches))
            candidates = []
            for (_, record), score in zip(matches, scores, strict=True):
                item = dict(record)
                item.pop("embedding", None)
                if output_fields is not None:
                    item = {key: item[key] for key in output_fields if key in item}
                item["score"] = score
                candidates.append((score, item))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in candidates[:top_k]]

    def count(self, filters: dict[str, Any]) -> int:
        """聚合计数（不含已删除记录），与 search 的过滤语义一致。

        TASK-006：供 memory_stats 等统计路径替代全量拉取后 Python 计数。
        """
        merged_filters = {"is_deleted": False, **filters}
        with self._lock:
            return sum(
                1 for record in self._records.values()
                if not record.get("is_deleted") and self._matches_filters(record, merged_filters)
            )

    def list_page(
        self,
        filters: dict[str, Any],
        *,
        offset: int = 0,
        limit: int = 20,
        output_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """稳定排序分页（TASK-014 / Issue #13）：创建时间倒序。

        本地后端 dict 插入序等价创建序，倒序切片精确稳定，单次扫描完成；
        与 MilvusClient.list_page 的两阶段实现语义一致。
        """
        if limit < 1 or offset < 0:
            raise ValueError("分页参数不合法：limit>=1 且 offset>=0")
        merged_filters = {"is_deleted": False, **filters}
        with self._lock:
            matches = [
                record
                for record in self._records.values()
                if not record.get("is_deleted") and self._matches_filters(record, merged_filters)
            ]
            page_records = [dict(record) for record in list(reversed(matches))[offset : offset + limit]]
        items = []
        for record in page_records:
            item = dict(record)
            item.pop("embedding", None)
            if output_fields is not None:
                item = {key: item[key] for key in output_fields if key in item}
            items.append(item)
        return {"total": len(matches), "offset": offset, "limit": limit,
                "returned": len(items), "items": items, "order": "created_desc"}

    def get_by_id(
        self,
        memory_id: str,
        *,
        include_embedding: bool = False,
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(memory_id)
            if record is None or record.get("is_deleted"):
                return None
            result = dict(record)
            if not include_embedding:
                result.pop("embedding", None)
            elif isinstance(result.get("embedding"), list):
                result["embedding"] = list(result["embedding"])
        return result

    def update(
        self,
        memory_id: str,
        fields: dict[str, Any],
        new_embedding: list[float] | None = None,
    ) -> bool:
        with self._lock:
            record = self._records.get(memory_id)
            if record is None or record.get("is_deleted"):
                return False
            record.update(fields)
            if new_embedding is not None:
                record["embedding"] = list(new_embedding)
            if new_embedding is not None or "embedding" in fields or "is_deleted" in fields:
                vector = record["embedding"]
                if isinstance(vector, list):
                    record["embedding"] = list(vector)
                self._norms[memory_id] = _norm(vector) if isinstance(vector, list) else 0.0
                self._invalidate_matrix()
        return True

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        with self._lock:
            record = self._records.get(memory_id)
            if record is None or record.get("is_deleted"):
                return False
            if soft:
                record["is_deleted"] = True
            else:
                del self._records[memory_id]
            self._norms.pop(memory_id, None)
            self._invalidate_matrix()
        return True

    def delete_by_filters(
        self,
        filters: dict[str, Any],
        soft: bool = True,
    ) -> list[str]:
        with self._lock:
            memory_ids = [
                memory_id for memory_id, record in self._records.items()
                if not record.get("is_deleted") and self._matches_filters(record, filters)
            ]
            for memory_id in memory_ids:
                self.delete(memory_id, soft=soft)
        return memory_ids

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._norms.clear()
            self._invalidate_matrix()
