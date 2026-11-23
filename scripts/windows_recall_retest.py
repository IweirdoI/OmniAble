"""Compare TASK-038 recall on two checkouts with a shared BGE-M3 fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from random import Random
from statistics import median
import sys
from threading import Barrier
from time import perf_counter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--vectors", type=Path, required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--backend", choices=["memory", "milvus-lite"], default="memory")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--warm-cache", action="store_true")
    args = parser.parse_args()
    if not args.prepare and (args.output is None or (args.backend == "milvus-lite" and args.database is None)):
        parser.error("measurement requires --output, and Milvus Lite also requires --database")
    args.vectors = args.vectors.resolve()
    if args.output:
        args.output = args.output.resolve()
    if args.database:
        args.database = args.database.resolve()
        if args.database.exists():
            parser.error("use a fresh Milvus Lite database for each run")

    args.workspace = args.workspace.resolve()
    os.chdir(args.workspace)
    sys.path.insert(0, str(args.workspace))
    os.environ.update(
        OS_AGENT_EMBEDDING_BACKEND="local",
        OS_AGENT_MEMORY_BACKEND="milvus" if args.backend == "milvus-lite" else "memory",
        OS_AGENT_EMBEDDING_CACHE_SIZE="1024" if args.warm_cache else "0",
        OS_AGENT_EMBEDDING_SERIALIZE="0",
        OMP_NUM_THREADS="4",
        OPENBLAS_NUM_THREADS="1",
        HF_HUB_OFFLINE="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
    )
    if args.database:
        os.environ["OS_AGENT_MILVUS_URI"] = str(args.database)
        os.environ["OS_AGENT_MILVUS_COLLECTION"] = "task038_retest"

    import numpy as np
    import torch
    from benchmarks.run_benchmarks import _percentile, _retrieval_corpus
    from huggingface_hub import snapshot_download
    from scripts.windows_perf_acceptance import _FixedVectorEmbedding, _scale_content, _total_ram_gb
    from src.common.models import RetrievalQuery
    from src.core.factory import create_orchestrator
    from src.embedding.embedding_interface import get_shared_embedding_backend, load_embedding_config
    from src.knowledge.memory_service.create_memory import create_memory
    from src.knowledge.memory_service.memory_schema import (
        CreateMemoryRequest, MemoryEntry, MemorySource, MemoryTypeTag, TaskType,
    )

    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    model_path = snapshot_download("BAAI/bge-m3", revision=args.model_revision, local_files_only=True)
    load_embedding_config().update(device="cpu", local_model=model_path, allow_stub_fallback=False)
    provider = get_shared_embedding_backend()
    started = perf_counter()
    probe = provider.embed(["TASK-038 本地模型预检"])[0]
    model_start_ms = (perf_counter() - started) * 1000
    if provider._fallback_mode or len(probe) != 1024:
        raise RuntimeError("expected real BGE-M3 with 1024 dimensions")

    gold_docs, gold_queries = _retrieval_corpus()
    rng = Random(42)
    scale_docs = [_scale_content(rng, i) for i in range(1000)]
    docs = gold_docs + scale_docs
    corpus_hash = hashlib.sha256(json.dumps(docs, ensure_ascii=False).encode()).hexdigest()
    if args.prepare:
        vectors = []
        for offset in range(0, len(docs), 32):
            vectors.extend(provider.embed(docs[offset:offset + 32]))
        np.savez(args.vectors, vectors=np.asarray(vectors, dtype=np.float32),
                 corpus_hash=corpus_hash, revision=args.model_revision)
        print(f"Prepared {len(vectors)} vectors", flush=True)
        return 0

    with np.load(args.vectors, allow_pickle=False) as fixture:
        if (fixture["corpus_hash"].item() != corpus_hash or fixture["revision"].item() != args.model_revision
                or fixture["vectors"].shape != (len(docs), 1024)):
            raise ValueError("vector fixture does not match this corpus and model revision")
        embedding = _FixedVectorEmbedding(fixture["vectors"].tolist())

    orchestrator = create_orchestrator()
    for i, doc in enumerate(docs):
        gold = i < len(gold_docs)
        result = create_memory(CreateMemoryRequest(entry=MemoryEntry(
            content=doc, user_id="qa_gold" if gold else "qa_scale",
            memory_type=MemoryTypeTag.TOOL_CALL if gold else MemoryTypeTag.DOCUMENT_CHUNK,
            task_type=TaskType.CODING, source=MemorySource.MANUAL, confidence=0.9,
        )), milvus=orchestrator._milvus, embedding=embedding)
        if not result.created:
            raise RuntimeError("failed to populate the shared corpus")
    print(f"{args.backend}: populated {len(docs)} entries", flush=True)

    def recall(query: str, user: str = "qa_scale", **kwargs):
        return orchestrator.recall(RetrievalQuery(query=query, user_id=user, top_k=5), **kwargs)

    def timed(query: str) -> float:
        start = perf_counter()
        recall(query)
        return (perf_counter() - start) * 1000

    def stats(samples: list[float]) -> dict:
        return {"samples_ms": samples, "p50_ms": median(samples),
                "p95_ms": _percentile(samples, 95), "max_ms": max(samples)}

    quality = []
    for query, target in gold_queries:
        texts = [item.content for item in recall(query, "qa_gold").items]
        quality.append({"hit5": any(target in text for text in texts), "hit1": bool(texts and target in texts[0])})

    serial_index = rng.randrange(len(scale_docs))
    serial_query = scale_docs[serial_index][:120]
    first_serial_ms = timed(serial_query)
    serial = stats([timed(serial_query) for _ in range(10)])
    rounds = 5
    query_indices = [[(worker * 7 + turn * 13) % len(scale_docs) for turn in range(rounds)]
                     for worker in range(4)]
    if args.warm_cache:
        for indices in query_indices:
            for index in indices:
                recall(scale_docs[index][:120])

    barrier = Barrier(4)

    def worker(indices):
        barrier.wait(timeout=30)
        results = []
        for index in indices:
            start = perf_counter()
            error = None
            try:
                recall(scale_docs[index][:120])
            except Exception as exc:
                error = type(exc).__name__
            results.append({"index": index, "latency_ms": (perf_counter() - start) * 1000,
                            "ok": error is None, "error_type": error})
        return results

    with ThreadPoolExecutor(max_workers=4) as pool:
        start = perf_counter()
        batches = list(pool.map(worker, query_indices))
        wall_s = perf_counter() - start
    samples = [sample for batch in batches for sample in batch]
    traces = []
    if hasattr(provider, "cache_stats"):
        for index in query_indices[0]:
            trace = {}
            recall(scale_docs[index][:120], trace=trace)
            traces.append({"index": index, "stages": trace})

    payload = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "env": {"os": platform.platform(), "processor": platform.processor(), "ram_gb": _total_ram_gb(),
                "python": platform.python_version(), "torch": torch.__version__,
                "sentence_transformers": version("sentence-transformers"), "numpy": np.__version__,
                "pymilvus": version("pymilvus"), "backend": args.backend,
                "milvus_lite": version("milvus-lite") if args.database else None,
                "model": "BAAI/bge-m3", "model_revision": args.model_revision,
                "device": "cpu", "dimension": len(probe), "fallback": False,
                "torch_threads": torch.get_num_threads(), "torch_interop_threads": torch.get_num_interop_threads(),
                "cache": "prewarmed" if args.warm_cache else "off"},
        "corpus": {"sha256": corpus_hash, "seed": 42, "gold_docs": len(gold_docs), "scale": len(scale_docs)},
        "model_start_ms": model_start_ms,
        "quality": {"recall_at_5": sum(row["hit5"] for row in quality) / len(quality),
                    "hit_at_1": sum(row["hit1"] for row in quality) / len(quality), "queries": quality},
        "serial": {"index": serial_index, "first_ms": first_serial_ms, **serial},
        "concurrency": {"workers": 4, "rounds": rounds, "wall_s": wall_s, "ops_per_sec": len(samples) / wall_s,
                        "failure_rate": sum(not sample["ok"] for sample in samples) / len(samples),
                        "worker_samples": batches, **stats([sample["latency_ms"] for sample in samples])},
        "phase_traces": traces,
    }
    if sys.platform == "win32":
        payload["peak_working_set_bytes"] = int(subprocess.check_output([
            "powershell.exe", "-NoProfile", "-Command",
            f"[Diagnostics.Process]::GetProcessById({os.getpid()}).PeakWorkingSet64",
        ], text=True).strip())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{args.backend}: serial P95={serial['p95_ms']:.1f} ms", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
