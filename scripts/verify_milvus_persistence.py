#!/usr/bin/env python3
"""Milvus 持久化重启验证（TASK-007 / Issue #6：需要重启持久化时单独验证）.

步骤（对应验收要求的「建库、退出、新进程重连和检索命中」）：
  1) 建库+播种：运行 benchmarks/run_benchmarks.py --real 或
     scripts/windows_perf_acceptance.py（OS_AGENT_MEMORY_BACKEND=milvus）
  2) 退出进程
  3) 新进程重连本脚本：python scripts/verify_milvus_persistence.py \\
        --uri data/perf_milvus_bench.db --collection mokoa_windows_perf
  4) 断言记录数 > 0、命中指定记录且无 embedding 泄漏

 milvus-lite 用本地文件 URI；远端 Milvus 用 http://host:19530（需先 load，
 由 MilvusClient._ensure_collection 的 load_collection 修复保证）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.embedding.embedding_interface import get_shared_embedding_backend  # noqa: E402
from src.knowledge.memory_service.milvus_client import MilvusClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Milvus 持久化重启验证")
    parser.add_argument("--uri", default="data/perf_milvus_bench.db",
                        help="milvus-lite 文件路径或 http://host:19530")
    parser.add_argument("--collection", default="mokoa_windows_perf")
    parser.add_argument("--query", default="pip 镜像怎么设置",
                        help="重连后用于检索命中的查询")
    parser.add_argument("--expected-content", default="pip 安装超时时应使用国内镜像源",
                        help="必须在重连后的检索结果中出现的播种记录")
    args = parser.parse_args()

    # 全新进程：与本机已存在的任何连接无关，专测「重启后数据仍在」
    client = MilvusClient(uri=args.uri, collection=args.collection)
    provider = get_shared_embedding_backend()

    total = client.count({})
    vector = provider.embed([args.query])[0]
    hits = client.search(vector, {}, top_k=5)
    expected_hit = any(hit.get("content") == args.expected_content for hit in hits)
    embedding_leak = any("embedding" in hit for hit in hits)

    result = {
        "uri": args.uri,
        "collection": args.collection,
        "persisted_records_total": total,
        "reconnect_search_hits": len(hits),
        "hit_contents_prefix": [h.get("content", "")[:40] for h in hits],
        "expected_content_hit": expected_hit,
        "embedding_leak": embedding_leak,
        "verdict": "PASS" if total > 0 and expected_hit and not embedding_leak else "FAIL",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    (Path(_ROOT) / "benchmarks" / "results").mkdir(parents=True, exist_ok=True)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
