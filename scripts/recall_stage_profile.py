#!/usr/bin/env python3
"""TASK-038 阶段计时探针：在完整 recall 链路内直测各阶段（Issue #47）.

按 Issue 要求「在 1k 语料完整 recall 链路内分别测量词面候选读取、词面匹配、
向量查询与编码，保留原样本和阶段统计。先用直接测量验证瓶颈排序」。

复用 orchestrator.recall 的 trace 打点（同一实现），在固定语料上重复采样，
输出每阶段的 P50/P95 与候选量。默认 memory 后端 + hash 仅接线；
正式数据请设 OS_AGENT_EMBEDDING_BACKEND=local。

运行：
    python scripts/recall_stage_profile.py --scale 1000 --reps 10
    OS_AGENT_EMBEDDING_BACKEND=local python scripts/recall_stage_profile.py --scale 1000
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

STAGES = (
    "vector_query", "vector_convert", "intent", "expanded_query",
    "rewrite_query", "lexical_fetch", "lexical_match", "merge", "rerank",
)


def _pct(values: list[float], p: float) -> float:
    from benchmarks.run_benchmarks import _percentile
    return _percentile(values, p)


def main() -> int:
    parser = argparse.ArgumentParser(description="recall 阶段计时探针")
    parser.add_argument("--scale", type=int, default=1000)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default="benchmarks/results")
    args = parser.parse_args()

    from src.core.factory import create_orchestrator
    from src.common.models import RetrievalQuery
    from src.knowledge.memory_service.create_memory import create_memory
    from src.knowledge.memory_service.embedding_client import EmbeddingClient
    from src.knowledge.memory_service.memory_schema import (
        CreateMemoryRequest, MemoryEntry, MemorySource, MemoryTypeTag, TaskType,
    )
    from benchmarks.run_benchmarks import _retrieval_corpus

    print(f"[1/3] 构建 orchestrator（backend={os.getenv('OS_AGENT_MEMORY_BACKEND','auto')}, "
          f"embedding={os.getenv('OS_AGENT_EMBEDDING_BACKEND','auto')}）...", flush=True)
    orchestrator = create_orchestrator()
    embedding = EmbeddingClient()
    client = orchestrator._milvus

    # 金标语料（与 --real 同源）
    documents, relevant = _retrieval_corpus()
    gold_user = f"profile_gold_{os.getpid()}"
    from scripts.windows_perf_acceptance import _FixedVectorEmbedding

    # 批量编码后逐条入库：create_memory 内部是单条编码，逐条调用会慢 5-10 倍
    # （TASK-038 实测：1000 条单条编码 >20 分钟）。批量编码不改入库语义。
    print(f"[2/3] 批量编码并播种：金标 {len(documents)} + 规模 {args.scale} 条 ...", flush=True)
    rng = random.Random(args.seed)
    fillers = ["项目操作案例", "终端命令输出", "文档片段摘要", "工具调用结果",
               "系统配置快照", "办公助手上下文", "服务日志片段", "用户操作行为"]
    scale_user = f"profile_scale_{os.getpid()}"
    docs = [f"规模语料第 {i} 号：" + "；".join(rng.choice(fillers) for _ in range(rng.randint(8, 20)))
            for i in range(args.scale)]

    gold_vectors = embedding._provider.embed(documents)
    scale_vectors = embedding._provider.embed(docs)
    for doc, vec in zip(documents, gold_vectors, strict=True):
        create_memory(CreateMemoryRequest(entry=MemoryEntry(
            content=doc, memory_type=MemoryTypeTag.TOOL_CALL, task_type=TaskType.CODING,
            source=MemorySource.MANUAL, user_id=gold_user, confidence=0.9,
        )), milvus=client, embedding=_FixedVectorEmbedding([vec]))
    scale_embedding = _FixedVectorEmbedding(scale_vectors)
    for doc in docs:
        create_memory(CreateMemoryRequest(entry=MemoryEntry(
            content=doc, memory_type=MemoryTypeTag.DOCUMENT_CHUNK, task_type=TaskType.CODING,
            source=MemorySource.MANUAL, user_id=scale_user, confidence=0.9,
        )), milvus=client, embedding=scale_embedding)

    # 采样：知识类查询触发词面补召（与 TASK-007 一致）
    queries = [q for q, _ in relevant] + [docs[i][:120] for i in
                                          rng.sample(range(len(docs)), min(5, len(docs)))]
    print(f"[3/3] 采样 {args.reps} 轮 × {len(queries)} 查询 ...", flush=True)

    stage_samples: dict[str, list[float]] = {name: [] for name in STAGES}
    # query_memory 内部拆分（Issue #47 要求编码与向量查询分别测量）
    split_samples: dict[str, list[float]] = {"encode": [], "vector_search": []}
    totals: list[float] = []
    candidates: dict[str, list[int]] = {}
    for _ in range(args.reps):
        for query in queries:
            trace: dict = {}
            orchestrator.recall(
                RetrievalQuery(query=query, user_id=scale_user, top_k=5), trace=trace
            )
            for name in STAGES:
                entry = trace.get(name)
                if entry and "ms" in entry:
                    stage_samples[name].append(entry["ms"])
                    if "candidates" in entry:
                        candidates.setdefault(name, []).append(entry["candidates"])
                    if "pool" in entry:
                        candidates.setdefault(name, []).append(entry["pool"])
                    # vector_query 阶段内的编码/检索拆分（含 rewrite 那次）
                    if entry.get("embedding_ms") is not None:
                        split_samples["encode"].append(entry["embedding_ms"])
                    if entry.get("search_ms") is not None:
                        split_samples["vector_search"].append(entry["search_ms"])
            totals.append(trace.get("total", {}).get("ms", 0.0))

    summary = {}
    for name in STAGES:
        values = stage_samples[name]
        if not values:
            continue
        summary[name] = {
            "samples": len(values),
            "p50_ms": round(_pct(values, 50), 2),
            "p95_ms": round(_pct(values, 95), 2),
            "mean_ms": round(statistics.fmean(values), 2),
            "max_ms": round(max(values), 2),
        }
        if name in candidates:
            summary[name]["median_candidates"] = int(statistics.median(candidates[name]))
    for name, values in split_samples.items():
        if values:
            summary[name] = {
                "samples": len(values),
                "p50_ms": round(_pct(values, 50), 2),
                "p95_ms": round(_pct(values, 95), 2),
                "mean_ms": round(statistics.fmean(values), 2),
                "max_ms": round(max(values), 2),
            }

    total_p95 = round(_pct(totals, 95), 2)
    if totals:
        summary["total"] = {
            "samples": len(totals),
            "p50_ms": round(_pct(totals, 50), 2),
            "p95_ms": total_p95,
            "mean_ms": round(statistics.fmean(totals), 2),
            "max_ms": round(max(totals), 2),
        }
    ordered = sorted(
        ((k, v) for k, v in summary.items() if k != "total"),
        key=lambda kv: kv[1]["p50_ms"], reverse=True,
    )

    print("\n== 阶段耗时（按 P50 降序）==")
    accounted = 0.0
    for name, stat in ordered:
        share = stat["p50_ms"] / summary["total"]["p50_ms"] * 100 if summary.get("total") else 0
        if name != "total":
            accounted += stat["p50_ms"]
        cand = f"  candidates~{stat['median_candidates']}" if "median_candidates" in stat else ""
        print(f"  {name:16s} P50={stat['p50_ms']:8.2f}ms  P95={stat['p95_ms']:8.2f}ms  "
              f"share={share:5.1f}%{cand}")
    print(f"\n完整 recall P50={summary.get('total',{}).get('p50_ms')}ms  "
          f"P95={total_p95}ms  （预算 500ms）")
    print(f"已直测阶段合计 P50={accounted:.2f}ms，"
          f"未归因={summary.get('total',{}).get('p50_ms',0) - accounted:.2f}ms")

    out_dir = _ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"recall_stage_profile_{ts}.json"
    path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scale": args.scale, "reps": args.reps, "seed": args.seed,
        "embedding_backend": os.getenv("OS_AGENT_EMBEDDING_BACKEND", "auto"),
        "memory_backend": os.getenv("OS_AGENT_MEMORY_BACKEND", "auto"),
        "stages": summary, "total_p95_ms": total_p95,
        "accounted_p50_ms": round(accounted, 2),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
