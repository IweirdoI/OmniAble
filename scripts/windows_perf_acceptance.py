#!/usr/bin/env python3
"""TASK-007 Windows 桌面应用目标后端性能验收（分阶段耗时/并发/失败率/冷暖启动）.

目标配置见 docs/WINDOWS.md「TASK-007 目标配置验收」——由调用方设置环境变量：
    OS_AGENT_EMBEDDING_BACKEND=local            # BGE-M3（语义验收必须 local）
    OS_AGENT_MEMORY_BACKEND=memory|milvus       # milvus 时另设 OS_AGENT_MILVUS_URI
    OS_AGENT_BENCHMARK_COLLECTION=mokoa_windows_benchmark   # milvus 独立评测集合

流程（同一提交、同一金标、同一配置）：
  1) factory.create_orchestrator() 按目标配置构建（与应用同一条装配路径）
  2) 固定金标语料播种（真实编码，模型首次加载单独计时）
  3) 规模语料播种（100/1000/…，含插入吞吐）
  4) 冷启动：检索链路首次完整 recall 计时（模型加载已单列）
  5) 分阶段耗时（warm）：encode / storage 直测共享组件；
     rerank_profile ≈ recall_total − encode − storage（近似口径，含词面补召）
  6) Recall@5 / Hit@1：与 run_benchmarks --real 相同金标与计分约定（不改动金标）
  7) 并发：workers × rounds 完整 recall，P50/P95/吞吐/失败率
  8) chat：需可用模型服务；无密钥/服务时单列缺测

输出: benchmarks/results/perf_acceptance_<ts>.json / .md
退出码: 0 = 全部测量完成（不代表达标）；口径达标与否在报告里如实呈现。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 模型下载走镜像（首次运行下载 BGE-M3）；调用方已显式设置时不覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

RESULTS_DIR = _ROOT / "benchmarks" / "results"
LATENCY_BUDGET_MS = 500


def _perf() -> float:
    return time.perf_counter()


def _git_commit() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(_ROOT),
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _total_ram_gb() -> float | None:
    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return round(stat.ullTotalPhys / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001 - 环境探测失败不阻断
        return None


def _scale_content(rng: random.Random, index: int) -> str:
    fillers = [
        "项目操作案例", "终端命令输出", "文档片段摘要", "工具调用结果",
        "系统配置快照", "办公助手上下文", "服务日志片段", "用户操作行为",
    ]
    body = "；".join(rng.choice(fillers) for _ in range(rng.randint(8, 20)))
    return f"规模语料第 {index} 号：{body}"[:600]


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-007 目标后端性能验收")
    parser.add_argument("--scale", type=int, default=1000, help="规模语料条数")
    parser.add_argument("--reps", type=int, default=10, help="分阶段测量的 warm 重复次数")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument("--rounds", type=int, default=5, help="每线程并发轮数")
    parser.add_argument("--seed", type=int, default=42, help="规模语料随机种子")
    parser.add_argument("--collection", type=str, default="mokoa_windows_perf",
                        help="milvus 模式使用的独立评测集合")
    parser.add_argument("--allow-hash", action="store_true",
                        help="允许 hash 向量干跑（仅接线验证，不代表语义验收）")
    parser.add_argument("--out", type=str, default="benchmarks/results")
    args = parser.parse_args()

    backend = os.getenv("OS_AGENT_EMBEDDING_BACKEND", "auto")
    if backend != "local" and not args.allow_hash:
        raise SystemExit(
            "TASK-007 语义验收要求 OS_AGENT_EMBEDDING_BACKEND=local；"
            "hash 仅为接线对照，确需干跑请加 --allow-hash"
        )
    if os.getenv("OS_AGENT_MEMORY_BACKEND", "").lower() == "milvus":
        # 独立评测集合：不污染应用集合（与 run_benchmarks --real 的隔离约定一致）
        os.environ.setdefault("OS_AGENT_MILVUS_COLLECTION", args.collection)

    from src.core.factory import create_orchestrator, resolve_memory_backend
    from src.embedding.embedding_interface import (
        get_shared_embedding_backend,
        resolve_embedding_backend_name,
    )
    from src.knowledge.memory_service.embedding_client import EmbeddingClient
    from src.knowledge.memory_service.memory_schema import (
        CreateMemoryRequest,
        MemoryEntry,
        MemorySource,
        MemoryTypeTag,
        TaskType,
    )
    from src.knowledge.memory_service.create_memory import create_memory
    from src.common.models import RetrievalQuery
    from benchmarks.run_benchmarks import _retrieval_corpus, _percentile

    orchestrator = create_orchestrator()
    client = orchestrator._milvus
    provider = get_shared_embedding_backend()
    # create_memory 的 embedding 参数约定为 EmbeddingClient（单条字符串入参），
    # 共享底层 provider 与应用完全一致（warm 复用，无重复加载）。
    embedding_client = EmbeddingClient()

    embedding_backend = resolve_embedding_backend_name()
    storage_cfg = {"memory_backend": os.getenv("OS_AGENT_MEMORY_BACKEND", "auto")}
    memory_backend = resolve_memory_backend(storage_cfg, project_root=_ROOT)
    model_name = getattr(provider, "_model_name", None) or embedding_backend

    documents, relevant = _retrieval_corpus()
    gold_user = f"perf_gold_{os.getpid()}"
    scale_user = f"perf_scale_{os.getpid()}"

    # ---- 金标语料播种（真实编码；首次真实编码单独计时 = 模型加载口径） ----
    model_load_start = _perf()
    first_vec = provider.embed([documents[0]])[0]
    model_load_s = _perf() - model_load_start  # 含模型加载/下载检查（冷）
    dim = len(first_vec)

    # fallback 判定必须在首次实际编码之后：local 模型加载失败回退 stub 的
    # _fallback_mode 只在 _ensure_model 尝试后才翻转；接线干跑不给正式判定
    fallback = embedding_backend == "hash" or bool(
        getattr(provider, "_fallback_mode", False)
    )
    formal = not fallback

    gold_created = 0
    for doc in documents:
        entry = MemoryEntry(
            content=doc,
            memory_type=MemoryTypeTag.TOOL_CALL,
            task_type=TaskType.CODING,
            source=MemorySource.MANUAL,
            user_id=gold_user,
            confidence=0.9,
        )
        if create_memory(
            CreateMemoryRequest(entry=entry), milvus=client, embedding=embedding_client
        ).created:
            gold_created += 1

    # ---- 规模语料播种（批量编码 + 逐条 create_memory，含插入吞吐） ----
    rng = random.Random(args.seed)
    scale_docs = [_scale_content(rng, i) for i in range(args.scale)]
    scale_encode_start = _perf()
    scale_vecs = provider.embed(scale_docs)
    scale_encode_s = _perf() - scale_encode_start
    insert_start = _perf()
    scale_created = 0
    fixed_embedding = _FixedVectorEmbedding(scale_vecs)
    for doc in scale_docs:
        entry = MemoryEntry(
            content=doc,
            memory_type=MemoryTypeTag.DOCUMENT_CHUNK,
            task_type=TaskType.CODING,
            source=MemorySource.MANUAL,
            user_id=scale_user,
            confidence=0.9,
        )
        resp = create_memory(
            CreateMemoryRequest(entry=entry), milvus=client, embedding=fixed_embedding
        )
        if resp.created:
            scale_created += 1
    insert_s = _perf() - insert_start

    probe_queries = [query for query, _ in relevant]
    sample_query = probe_queries[0]
    sample_vector = provider.embed([sample_query])[0]

    # ---- 冷启动：检索链路首次完整 recall（模型加载已单列） ----
    cold_start = _perf()
    cold_result = orchestrator.recall(
        RetrievalQuery(query=sample_query, user_id=gold_user, top_k=5)
    )
    cold_first_recall_ms = (_perf() - cold_start) * 1000
    cold_items = len(cold_result.items)

    # ---- 分阶段耗时（warm） ----
    def _measure(fn, reps: int) -> dict:
        latencies = []
        for _ in range(reps):
            start = _perf()
            fn()
            latencies.append((_perf() - start) * 1000)
        return {
            "reps": reps,
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(_percentile(latencies, 95), 3),
            "mean_ms": round(statistics.fmean(latencies), 3),
            "raw_ms": [round(v, 3) for v in latencies],
        }

    encode_latency = _measure(lambda: provider.embed([sample_query]), args.reps)
    storage_latency = _measure(
        lambda: client.search(sample_vector, {"user_id": gold_user}, top_k=5),
        args.reps,
    )
    recall_latency = _measure(
        lambda: orchestrator.recall(
            RetrievalQuery(query=sample_query, user_id=gold_user, top_k=5)
        ),
        args.reps,
    )
    # 词面补召拉取实测：recall 在 knowledge 词面路径会用
    # search(None, filters, top_k=10_000) 全量拉取匹配记录（此前为残差估算，
    # 队长复核要求改为实测）。这里以同一过滤条件直测该阶段。
    lexical_fetch_latency = _measure(
        lambda: client.search(None, {"user_id": gold_user}, top_k=10_000),
        args.reps,
    )
    rerank_profile_latency = {
        key: round(
            max(recall_latency[key] - encode_latency[key] - storage_latency[key], 0.0), 3
        )
        for key in ("p50_ms", "p95_ms", "mean_ms")
    }
    # 规模范围（scale_user，args.scale 条）的完整 recall：Issue #6 的
    # 「数据规模 + P95 对照 500ms」按真实规模口径测量
    scale_query = scale_docs[rng.randrange(len(scale_docs))][:120]
    recall_at_scale_latency = _measure(
        lambda: orchestrator.recall(
            RetrievalQuery(query=scale_query, user_id=scale_user, top_k=5)
        ),
        args.reps,
    )
    lexical_fetch_at_scale_latency = _measure(
        lambda: client.search(None, {"user_id": scale_user}, top_k=10_000),
        args.reps,
    )

    # ---- Recall@5 / Hit@1（完整 recall 链路，与 --real 同金标同计分） ----
    hits_at5 = 0
    hits_at1 = 0
    recall_latencies: list[float] = []
    for query, relevant_content in relevant:
        start = _perf()
        result = orchestrator.recall(
            RetrievalQuery(query=query, user_id=gold_user, top_k=5)
        )
        recall_latencies.append((_perf() - start) * 1000)
        contents = [item.content for item in result.items]
        if any(relevant_content in c for c in contents):
            hits_at5 += 1
        if contents and relevant_content in contents[0]:
            hits_at1 += 1

    # ---- 并发（workers × rounds 完整 recall，规模范围 scale_user） ----
    latencies: list[float] = []
    failures: list[str] = []
    lock = threading.Lock()

    def _worker(wid: int) -> None:
        for r in range(args.rounds):
            query = scale_docs[(wid * 7 + r * 13) % len(scale_docs)][:120]
            start = _perf()
            try:
                orchestrator.recall(
                    RetrievalQuery(query=query, user_id=scale_user, top_k=5)
                )
            except Exception as exc:  # noqa: BLE001 - 失败率如实记录
                with lock:
                    failures.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            with lock:
                latencies.append((_perf() - start) * 1000)

    threads = [threading.Thread(target=_worker, args=(w,)) for w in range(args.workers)]
    wall_start = _perf()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall_s = _perf() - wall_start
    ops = len(latencies)
    concurrency = {
        "workers": args.workers,
        "rounds": args.rounds,
        "ops": ops,
        "wall_s": round(wall_s, 2),
        "ops_per_sec": round(ops / wall_s, 2) if wall_s else 0.0,
        "p50_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
        # P95 与分阶段测量统一走 _percentile（插值法）；此前把最大值写成 P95
        "p95_ms": round(_percentile(latencies, 95), 3) if latencies else 0.0,
        "max_ms": round(max(latencies), 3) if latencies else 0.0,
        "raw_ms": [round(value, 3) for value in latencies],
        "failure_rate": round(len(failures) / ops, 4) if ops else 0.0,
        "failure_samples": failures[:3],
    }

    # ---- chat：需要可用模型服务；缺密钥/服务时单列缺测 ----
    chat_probe: dict = {"status": "skipped",
                        "reason": "未配置模型服务密钥（SILICONFLOW_API_KEY / 本地 LM Studio），聊天耗时单列缺测"}
    if os.getenv("SILICONFLOW_API_KEY"):
        try:
            from fastapi.testclient import TestClient
            from api.server import app

            chat_client = TestClient(app)
            chat_start = _perf()
            response = chat_client.post("/agent/chat", json={
                "user_id": gold_user,
                "message": "用一句话说明 pip 超时怎么处理",
            })
            chat_ms = (_perf() - chat_start) * 1000
            chat_probe = {"status": "ran", "http_status": response.status_code,
                          "latency_ms": round(chat_ms, 1)} if response.status_code == 200 else {
                "status": "failed", "http_status": response.status_code}
        except Exception as exc:  # noqa: BLE001 - 聊天服务不可用如实记录
            chat_probe = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}

    environment = {
        "windows_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "total_ram_gb": _total_ram_gb(),
        "python_version": platform.python_version(),
        "git_commit": _git_commit(),
        "embedding_backend": embedding_backend,
        "embedding_model": model_name,
        "embedding_dim": dim,
        "embedding_fallback": fallback,
        "memory_backend": memory_backend,
        "milvus_uri": os.getenv("OS_AGENT_MILVUS_URI", "") if memory_backend == "milvus" else "",
        "model_load_s": round(model_load_s, 2),
        "scale_encode_s": round(scale_encode_s, 2),
        "scale_insert_s": round(insert_s, 2),
    }

    report = {
        "meta": {
            "project": "麒忆智脑 / MOKOA",
            "task": "TASK-007 / Issue #6（Windows 桌面应用目标后端性能验收）",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "embedding_backend": embedding_backend,
            "memory_backend": memory_backend,
            # 正式验收判定仅在 fallback=False（真实语义编码确认后）给出；
            # 接线干跑（hash / stub 降级）的数字仅验证链路，不带 PASS/FAIL
            "formal": formal,
            "run_kind": "正式验收" if formal else "接线干跑",
        },
        "environment": environment,
        "dataset": {
            # 实际加载计数：_retrieval_corpus 会追加 benchmarks/datasets/
            # knowledge_retrieval.jsonl 的条目（10 内置 docs + jsonl 扩展）
            "gold_corpus_docs": len(documents),
            "gold_queries": len(relevant),
            "gold_corpus_source": "run_benchmarks._retrieval_corpus（内置 + knowledge_retrieval.jsonl 扩展）",
            "scale_docs": args.scale,
            "scale_created": scale_created,
            "gold_created": gold_created,
            "note": "金标语料与 run_benchmarks --real 同源，计分约定一致，未改动金标",
        },
        "staged_latency": {
            "encode": encode_latency,
            "storage": storage_latency,
            "lexical_fetch": lexical_fetch_latency,
            "lexical_fetch_at_scale": lexical_fetch_at_scale_latency,
            "rerank_profile_approx": rerank_profile_latency,
            "recall_total": recall_latency,
            "recall_total_at_scale": recall_at_scale_latency,
            "chat": chat_probe,
            "note": "encode/storage/lexical_fetch 为直测；rerank_profile 为近似口径"
                    "（recall_total − encode − storage，含重排、合并与扩展查询等其余环节，"
                    "报告中标注为估算）；recall_total_at_scale 在规模语料范围测量",
        },
        "cold_start": {
            "model_load_s": round(model_load_s, 2),
            "first_recall_ms": round(cold_first_recall_ms, 3),
            "first_recall_items": cold_items,
        },
        "recall_quality": {
            "recall_at_5": round(hits_at5 / len(relevant), 4),
            "hit_at_1": round(hits_at1 / len(relevant), 4),
            "target": 0.85,
            "sample_size": len(relevant),
            "scoring": "与 run_benchmarks --real 相同金标与计分约定",
            # 接线干跑不给出达标判定（数字仅验证链路与口径）
            "passed": (hits_at5 / len(relevant)) >= 0.85 if formal else None,
        },
        "recall_latency_full": {
            # 预算对照口径：规模语料范围的完整 recall P95（Issue #6：以完整
            # recall 的 P95 对照 500ms 目标，按目标数据规模测量）。
            # P95 算法与全仓统一（_percentile 插值法），另单列 max 供参考
            "p50_ms": round(statistics.median(recall_at_scale_latency["raw_ms"]), 3),
            "p95_ms": round(_percentile(recall_at_scale_latency["raw_ms"], 95), 3),
            "max_ms": round(max(recall_at_scale_latency["raw_ms"]), 3),
            "budget_ms": LATENCY_BUDGET_MS,
            "passed_p95": (
                _percentile(recall_at_scale_latency["raw_ms"], 95) <= LATENCY_BUDGET_MS
                if formal else None
            ),
            "sample_size": len(recall_at_scale_latency["raw_ms"]),
            "scope": f"scale_user（{args.scale} 条语料）",
        },
        "concurrency": concurrency,
        "limitations": [
            "词面补召拉取（lexical_fetch）为直测；重排合并与其他环节为估算残差（recall_total − encode − lexical_fetch − storage）",
            "chat 需要可用模型服务，缺测时单列",
            "memory 后端为进程内接线对照；milvus 模式单独验证持久化",
        ],
    }

    out_dir = _ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"perf_acceptance_{ts}.json"
    md_path = out_dir / f"perf_acceptance_{ts}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(report), encoding="utf-8")
    print(_render_md(report))
    print(f"\n报告已写入：\n  {json_path}\n  {md_path}")
    return 0


class _FixedVectorEmbedding:
    """create_memory 兼容的 embedding 适配器：按调用次序返回预编码向量。

    create_memory 的 embedding 参数契约与 EmbeddingClient 一致——
    embed(content: str) 返回**单个**向量（不是向量列表）。
    本适配器按调用次序消费批量预编码的向量（顺序与播种循环一致）。
    """

    def __init__(self, vectors: list[list[float]]):
        self._vectors = vectors
        self._cursor = 0
        self._lock = threading.Lock()

    def embed(self, texts):
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        with self._lock:
            out = []
            for _ in items:
                vector = self._vectors[min(self._cursor, len(self._vectors) - 1)]
                self._cursor += 1
                out.append(list(vector))
        return out[0] if single else out


def _render_md(report: dict) -> str:
    env = report["environment"]
    formal = bool(report["meta"].get("formal"))
    # 正式判定只在 fallback=False（首次实际编码后确认）时给出；
    # 接线干跑只呈现数字，不标 PASS/FAIL（Task-007 队长复核口径）
    def _verdict(passed) -> str:
        if not formal or passed is None:
            return "接线干跑（无正式判定）"
        return "PASS" if passed else "FAIL"

    dataset = report["dataset"]
    lines = [
        "# TASK-007 Windows 桌面应用目标后端性能验收",
        "",
        f"- 生成时间：{report['meta']['generated_at']} ｜ 提交：{env.get('git_commit') or '未知'}",
        f"- 运行类型：**{report['meta'].get('run_kind')}**"
        + ("" if formal else "（fallback=hash/stub：数字仅验证链路，不给正式判定）"),
        f"- 环境：Windows {env['windows_version']} / {env['machine']} / {env['processor']} / RAM {env.get('total_ram_gb')}GB",
        f"- 配置：embedding={env['embedding_backend']}（{env['embedding_model']}，{env['embedding_dim']} 维，"
        f"fallback={env['embedding_fallback']}）｜ memory={env['memory_backend']}",
        f"- 模型加载：{env['model_load_s']}s ｜ 规模编码：{env['scale_encode_s']}s ｜ 插入：{env['scale_insert_s']}s",
        f"- 数据集：金标 {dataset['gold_corpus_docs']} docs / {dataset['gold_queries']} queries"
        f"（{dataset['gold_corpus_source']}）；规模语料 {dataset['scale_created']}/{dataset['scale_docs']} 条",
        "",
        "## 分阶段耗时（warm，P95 与全仓 _percentile 统一）",
        "",
        "| 阶段 | P50(ms) | P95(ms) | 均值(ms) |",
        "|---|---|---|---|",
    ]
    staged = report["staged_latency"]
    for name, label in (
        ("encode", "编码（embedding，直测）"),
        ("storage", "存储检索（向量检索直测）"),
        ("lexical_fetch", "词面补召拉取（标量 query 直测，金标范围）"),
        ("lexical_fetch_at_scale", f"词面补召拉取（规模 {dataset['scale_docs']} 条）"),
        ("rerank_profile_approx", "重排合并与其他（估算=recall−encode−storage）"),
        ("recall_total", "完整 recall 全链路（金标范围）"),
        ("recall_total_at_scale", f"完整 recall 全链路（规模 {dataset['scale_docs']} 条）"),
    ):
        metric = staged[name]
        lines.append(f"| {label} | {metric['p50_ms']} | {metric['p95_ms']} | {metric['mean_ms']} |")
    chat = staged["chat"]
    chat_note = json.dumps({k: v for k, v in chat.items() if k != "status"}, ensure_ascii=False)
    lines.append(f"| 聊天（chat） | status={chat.get('status')} {chat_note} | | |")
    full = report["recall_latency_full"]
    lines.append("")
    lines.append(f"- **完整 recall P95（{full.get('scope', '规模范围')}） = {full['p95_ms']}ms"
                 f"（预算 ≤{full['budget_ms']}ms，{_verdict(full.get('passed_p95'))}；"
                 f"max={full.get('max_ms')}ms）**，样本 {full['sample_size']}")
    quality = report["recall_quality"]
    lines.append(f"- Recall@5 = {quality['recall_at_5']} / Hit@1 = {quality['hit_at_1']} "
                 f"（目标 ≥{quality['target']}，金标 {quality['sample_size']} 条，"
                 f"达标判定：{_verdict(quality.get('passed'))}，与 --real 同计分）")
    conc = report["concurrency"]
    lines.append(f"- 并发 {conc['workers']} 线程 × {conc['rounds']} 轮：{conc['ops']} ops，"
                 f"{conc['ops_per_sec']} ops/s，P50={conc['p50_ms']}ms / P95={conc['p95_ms']}ms"
                 f"（max={conc.get('max_ms')}ms），失败率 {conc['failure_rate']}")
    for sample in conc["failure_samples"]:
        lines.append(f"  - 失败样例：{sample}")
    cold = report["cold_start"]
    lines.append(f"- 冷启动：模型加载 {cold['model_load_s']}s；检索链路首次 recall {cold['first_recall_ms']}ms"
                 f"（{cold['first_recall_items']} 条结果）")
    lines.append("")
    for note in report["limitations"]:
        lines.append(f"> {note}")
    if not formal:
        lines.append("> 本报告为接线干跑：fallback 状态在首次实际编码后确认，"
                     "不给出正式验收判定；正式判定请以 local 真实编码运行的报告为准。")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
