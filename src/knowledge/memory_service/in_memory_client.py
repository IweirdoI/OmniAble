"""Deterministic in-memory memory-service backend for tests and local demos.

This backend is deliberately separate from the real pymilvus wrapper so test
results cannot be mistaken for Milvus deployment or performance validation.
"""

from __future__ import annotations

import math
from typing import Any


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

    def insert(
        self,
        memory_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> bool:
        self._records[memory_id] = {
            "memory_id": memory_id,
            "content": content,
            "embedding": list(embedding),
            "is_deleted": False,
            **metadata,
        }
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
        """
        candidates: list[tuple[float, dict[str, Any]]] = []
        for record in self._records.values():
            if record.get("is_deleted"):
                continue
            if not self._matches_filters(record, filters):
                continue
            stored = record.get("embedding")
            score = (
                _cosine_similarity(embedding, stored)
                if embedding and isinstance(stored, list) and stored
                else 0.0
            )
            item = dict(record)
            item.pop("embedding", None)
            if output_fields is not None:
                # 只保留实际存储的字段（与 Milvus 服务端投影语义一致），
                # 不为缺失字段注入 None，避免改变调用方 metadata.get(k, default) 语义。
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
        return sum(
            1
            for record in self._records.values()
            if not record.get("is_deleted")
            and self._matches_filters(record, merged_filters)
        )

    def get_by_id(
        self,
        memory_id: str,
        *,
        include_embedding: bool = False,
    ) -> dict[str, Any] | None:
        record = self._records.get(memory_id)
        if record is None or record.get("is_deleted"):
            return None
        result = dict(record)
        if not include_embedding:
            result.pop("embedding", None)
        return result

    def update(
        self,
        memory_id: str,
        fields: dict[str, Any],
        new_embedding: list[float] | None = None,
    ) -> bool:
        record = self._records.get(memory_id)
        if record is None or record.get("is_deleted"):
            return False
        record.update(fields)
        if new_embedding is not None:
            record["embedding"] = list(new_embedding)
        return True

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        record = self._records.get(memory_id)
        if record is None or record.get("is_deleted"):
            return False
        if soft:
            record["is_deleted"] = True
        else:
            del self._records[memory_id]
        return True

    def delete_by_filters(
        self,
        filters: dict[str, Any],
        soft: bool = True,
    ) -> list[str]:
        memory_ids = [
            memory_id
            for memory_id, record in self._records.items()
            if not record.get("is_deleted")
            and self._matches_filters(record, filters)
        ]
        for memory_id in memory_ids:
            self.delete(memory_id, soft=soft)
        return memory_ids

    def clear(self) -> None:
        self._records.clear()
