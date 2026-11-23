
"""Query memory. Module: knowledge."""

from __future__ import annotations

import time

from src.knowledge.memory_service.embedding_client import EmbeddingClient
from src.knowledge.memory_service.memory_schema import QueryMemoryRequest, QueryMemoryResponse
from src.knowledge.memory_service.milvus_client import MilvusClient


def query_memory(
    request: QueryMemoryRequest,
    milvus: MilvusClient | None = None,
    embedding: EmbeddingClient | None = None,
) -> QueryMemoryResponse:
    start = time.perf_counter()
    milvus = milvus or MilvusClient()
    embedding = embedding or EmbeddingClient()

    embedding_ms: float | None = None
    search_ms: float | None = None
    if request.memory_id:
        item = milvus.get_by_id(request.memory_id)
        items = [item] if item else []
    else:
        filters = {"user_id": request.user_id, **request.filters}
        query = (request.query or "").strip()
        # 分项计时（TASK-038 / Issue #47）：编码与存储检索分开统计，
        # 让完整 recall 的瓶颈定位不依赖残差估算。
        vector = None
        if query:
            encode_start = time.perf_counter()
            vector = embedding.embed(query)
            embedding_ms = (time.perf_counter() - encode_start) * 1000
        search_start = time.perf_counter()
        items = milvus.search(vector, filters, top_k=request.top_k)
        search_ms = (time.perf_counter() - search_start) * 1000

    latency_ms = (time.perf_counter() - start) * 1000
    return QueryMemoryResponse(
        items=items,
        latency_ms=latency_ms,
        embedding_ms=embedding_ms,
        search_ms=search_ms,
    )
