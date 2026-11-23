# TASK-038 合入后双后端复测

2026-11-24，在 Windows 11 10.0.26200、AMD Ryzen 7 7840H（8 核 16 线程，约 27.7 GiB 可用物理内存）上复测。
同机对照 `e082d7e`（PR #50 合入前）与 `77464a7`（已包含 PR #50 的主线）。
Python 3.12.9、PyTorch 2.15.0+cpu、sentence-transformers 6.0.1、
pymilvus 2.6.17、Milvus Lite 3.2.2。PyTorch 使用 4 个算子线程和 1 个算子间线程。

模型固定为 BAAI/bge-m3 `5617a9f61b028005a4858fdac845db406aefb181`，
CPU 编码，1,024 维，`fallback=False`。
下载的权重与发布方提供的 SHA-256 一致。模型加载和文档播种在计时前完成。
[权重版本与校验值](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/pytorch_model.bin)

## 结果

| 场景与原始样本 | 首条规模查询 ms | 串行 P50 / P95 ms | 4 并发 P95 ms | 4 并发吞吐（请求/秒） | 峰值工作集 MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| [memory 优化前，无缓存](memory-baseline-off.json) | 1798.9 | 1706.9 / 1798.4 | 8141.0 | 0.56 | 2037 |
| [memory 合入版，无缓存](memory-candidate-off.json) | 800.1 | 817.6 / 861.1 | 3210.1 | 1.58 | 2052 |
| [memory 合入版，预热](memory-candidate-warm.json) | 840.8 | 430.7 / 537.9 | 1653.6 | 2.71 | 2016 |
| [Milvus Lite 优化前，无缓存](milvus-lite-baseline-off.json) | 1987.8 | 1989.7 / 2126.5 | 9776.5 | 0.55 | 2211 |
| [Milvus Lite 合入版，无缓存](milvus-lite-candidate-off.json) | 1211.5 | 1221.6 / 1242.6 | 4461.7 | 0.97 | 2215 |
| [Milvus Lite 合入版，预热](milvus-lite-candidate-warm.json) | 1287.8 | 870.8 / 962.7 | 3806.7 | 1.24 | 2219 |

六组 Recall@5、Hit@1 均为 **0.90**，120 次并发请求全部成功。按原始样本重算 P50、
P95 与 JSON 一致。关闭缓存后，memory 串行 P95 降低 **52.1%**，Milvus Lite 降低
**41.6%**。预热后，串行 P95 仍分别高于 500 ms 预算 **37.9 ms** 和 **462.7 ms**。
TASK-007 继续跟踪延迟达标及聊天耗时。

合入版无缓存的五次阶段探针显示，两后端的词面候选均为 1,000 条，向量候选中位数
均为 20 条。memory 的编码、词面拉取、词面匹配、重排中位数分别为
439.64、14.85、90.56、260.99 ms。Milvus Lite 分别为 440.88、310.22、86.17、
256.76 ms。该组数据下，Milvus Lite 的词面查库成本值得单独跟踪。

## 测量口径

- 固定 14 篇金标文档、10 条金标查询及 1,000 条规模语料，种子为 42。所有场景共用同一份
  BGE-M3 预编码文档向量，记录语料摘要。查询通过各代码版本正常的 recall 路径即时处理。
- 串行在同一检索词上记录首次调用和随后 10 次，P95 使用统一线性插值。并发使用 4 个线程，
  每线程依固定索引发起 5 次请求。原始样本记录耗时、索引和请求结果。
- 关闭缓存场景比较优化前后。预热场景在并发计时前预先运行这 20 条查询，用于测重复查询的命中收益。
  预热后的并发数据适用于向量已缓存的查询。Milvus Lite 每次运行都使用新的文件库。
- 合入版另外保留五条阶段探针的原始记录，计时主样本不启用探针。峰值工作集包含模型加载与播种。

## 复现命令

准备两个 Git 工作树以及一个本地 BGE-M3 缓存。验收脚本会读取固定模型版本，加载失败时停止。
将 `$env:HF_HUB_CACHE` 指向已下载该版本的 Hugging Face 缓存。复测时通过
`uv pip install --target <临时依赖目录> torch==2.15.0 sentence-transformers==6.0.1 milvus-lite==3.2.2`
准备隔离依赖，运行时将临时依赖目录加入 `PYTHONPATH`，基础依赖来自项目 Windows 虚拟环境。

```powershell
$python = (Resolve-Path .venv/Scripts/python.exe).Path
$retest = (Resolve-Path scripts/windows_recall_retest.py).Path
$out = New-Item -ItemType Directory (Join-Path ([IO.Path]::GetTempPath()) ('mokoa-retest-' + [guid]::NewGuid().ToString('N')))
$rev = '5617a9f61b028005a4858fdac845db406aefb181'
$baseline = Join-Path $out.FullName 'baseline'
$candidate = Join-Path $out.FullName 'candidate'
$vectors = Join-Path $out.FullName 'corpus.npz'

git worktree add --detach $baseline e082d7e
git worktree add --detach $candidate 77464a7
& $python $retest --workspace $candidate --vectors $vectors --model-revision $rev --prepare

foreach ($backend in @('memory', 'milvus-lite')) {
    foreach ($mode in @('baseline-off', 'candidate-off', 'candidate-warm')) {
        $checkout = if ($mode -eq 'baseline-off') { $baseline } else { $candidate }
        $name = "$backend-$mode"
        $flags = @('--workspace', $checkout, '--vectors', $vectors, '--model-revision', $rev,
                   '--backend', $backend, '--output', (Join-Path $out.FullName "$name.json"))
        if ($backend -eq 'milvus-lite') {
            $flags += @('--database', (Join-Path $out.FullName "$name.db"))
        }
        if ($mode -eq 'candidate-warm') { $flags += '--warm-cache' }
        & $python $retest @flags
        if ($LASTEXITCODE -ne 0) { throw "$name failed" }
    }
}
```
