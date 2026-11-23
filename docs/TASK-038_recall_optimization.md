# recall 词面补召与 CPU 编码优化（TASK-038 / Issue #47）

- 执行：HXFhappiness ｜ 日期：2026-11-23 ｜ 分支：`hxf/task-038-recall-profiling`
- 对照基线：TASK-007 / PR #46（`docs/Windows性能验收_20261122.md`、`docs/evidence/task-007-perf/`）
- 金标：沿用固定金标，未改动
- 同机双后端结果、原始样本与复现命令见 [合入后复测](evidence/task-038-retest/README.md)。

## 一、阶段直测（先测量再优化，Issue 要求）

新增 `orchestrator.recall(..., trace=...)` 分项打点（默认 None 零行为变化）与
`QueryMemoryResponse.embedding_ms/search_ms`（additive 可选字段），并由
`scripts/recall_stage_profile.py` 在**完整 recall 链路内**直测各阶段。

### 基线（优化前，BGE-M3 local + memory，500 条语料，5 轮 × 15 查询）

| 阶段 | P50 | P95 | 占比 | 候选量 |
|---|---|---|---|---|
| **编码（embedding）** | **127.5ms** | 276.6ms | **22.9%** | — |
| **向量检索** | **108.4ms** | 122.2ms | **19.5%** | — |
| **重排（rerank）** | **90.5ms** | 436.5ms | **16.3%** | — |
| **词面匹配（lexical_match）** | **44.7ms** | 206.7ms | **8.0%** | ~500 |
| 词面拉取（lexical_fetch） | 8.1ms | 10.2ms | 1.5% | ~500 |
| 其他（intent/convert/merge/expand） | <1ms | — | ~0% | — |
| **完整 recall** | **556.3ms** | **1041.6ms** | 100% | — |

**瓶颈排序（直测确认）**：编码 > 向量检索 > 重排 > 词面匹配 >> 词面拉取。
**再次验证**：词面「拉取」仅 8.1ms（1.5%），不是瓶颈——与 TASK-007 的实测结论一致。

## 二、优化实现（三项，均附等价性验证）

### 1. InMemory 向量检索向量化 + 范数/矩阵缓存

`InMemoryMilvusClient` 原实现逐条调用纯 Python `_cosine_similarity`，并且**每条都重算
查询范数与存储向量范数**（N 次重复）。改为：

- 插入/更新时预算并缓存存储向量范数
- 按需构建有效记录的 N×dim 矩阵。向量更新、删除和清空会释放旧矩阵；候选与打分在同一锁内读取。
- 一次矩阵乘得到全部打分；全量命中时零拷贝整矩阵相乘
- 维度不一致、numpy 缺失或 `OS_AGENT_INMEMORY_EXACT_COSINE=1` 时自动回退逐对实现

**等价性验证**（`verify_vec.py`，1000×1024 随机向量）：top10 **ID 与顺序完全一致**，
最大分数差 **4.16e-17**（浮点噪声级，不影响金标）。

### 2. 词面匹配查询侧预编译

`term_overlap` 原实现对每个候选重复计算「查询侧」（主题组识别、套话剥离后的
`distinctive_terms` 正则分词与 2~4 字片段展开、同义针展开），N 个候选即 N 次。
新增 `prepare_query_overlap()` 预编译一次，`lexical_hits` 与 `rerank_and_filter`
（rank_key 与词面过滤）对 N 个候选只做正文匹配。

**等价性验证**（`verify_overlap.py`）：16 条真实查询 × 15 条语料 + **3000 组模糊
随机组合**，共 3225 组，**零差异**。

### 3. 查询向量 LRU 缓存（并发策略按实测选择）

`LocalEmbeddingAdapter` 增加进程内 LRU 缓存（容量 `OS_AGENT_EMBEDDING_CACHE_SIZE`，
默认 1024，0 关闭）：

- 合入版只缓存检索查询，键为实例独立的带密钥摘要，缓存向量为不可变值。摄取、更新正文走普通编码。
- 模型名/维度/归一化在实例构造时固定 → 实例级缓存天然按「模型+维度+归一化」隔离
- 批量与单条调用共用同一缓存，批量时只对未命中项编码

**作者报告的并发编码实测**（BGE-M3 CPU，唯一查询避免缓存干扰；独立编码脚本与原始样本未随提交）：

| 场景 | P50 | P95 | 吞吐 |
|---|---|---|---|
| 1 串行 | 118.6ms | 119.1ms | 8.44 ops/s |
| 4 并发（不串行化） | 378.4ms | 687.9ms | 9.15 ops/s |
| 4 并发（串行化模型调用） | 116.9ms | **1487.9ms** | 7.73 ops/s |

**结论（按数据选择）**：4 并发吞吐仅比串行高 12%（CPU 争抢为主）；改用串行化后
P50 好看但**队尾 P95 恶化到 1488ms、吞吐更低**，对「P95 ≤ 500ms」目标更差。
因此**不采用串行化**（保留 `OS_AGENT_EMBEDDING_SERIALIZE` 开关作对照），
只启用查询向量缓存——这是该环境下唯一不牺牲 P95/吞吐的收益点。

## 三、优化效果

### 3.1 阶段级同口径对比（500 条、编码缓存关闭、5 轮 × 15 查询）

| 阶段 | 优化前 P50 | 优化后 P50 | 变化 |
|---|---|---|---|
| 向量检索 | 108.4ms | **3.1ms** | **-97%（35×）** |
| 词面匹配 | 44.7ms | **14.2ms** | **-68%** |
| 重排 | 90.5ms | **61.5ms** | **-32%** |
| 编码 | 127.5ms | 147.7ms | 持平（机器波动，未改动编码路径） |
| 词面拉取 | 8.1ms | 8.1→10.0ms | 持平 |
| **完整 recall P50** | **556.3ms** | **381.6ms** | **-31.4%** |
| **完整 recall P95** | **1041.6ms** | **588.1ms** | **-43.5%** |

### 3.2 目标规模 1000 条（TASK-007 harness，BGE-M3 local + memory，同语料/种子/配置）

| 指标 | 基线（TASK-007 / PR #46） | 优化后 | 变化 |
|---|---|---|---|
| **完整 recall P50** | 1652.8ms | **434.4ms** | **-73.7%** |
| **完整 recall P95** | 1825.6ms | **511.5ms** | **-72.0%**（距 500ms 差 11.5ms） |
| 存储检索（向量，直测）P50 | 3.3ms | **0.52ms** | -84% |
| 词面补召拉取（1000 条）P50 | 1.3ms | 1.5ms | 持平（本就不是瓶颈） |
| Recall@5 / Hit@1 | 0.9 / 0.9 | **0.9 / 0.9** | 不变 |
| 并发 4 线程 P95 | 7770.3ms | **3338.5ms** | **-57%** |
| 失败率 | 0 | 0 | — |

> 原实现中，固定金标 query 的重复编码探针可以命中缓存。1,000 条规模的串行探针也会
> 重复同一个规模查询，需分别记录缓存开启、关闭及多种查询的耗时，才能归因 CPU 编码成本。
> 合入版将直接编码探针与查询缓存分开；上表保留作者提交的历史结果，修订代码的性能仍待复测。

## 四、正确性回归

- 金标（`run_benchmarks.py --real`，BGE-M3 local + memory）：
  **Recall@5 = 1.0 / Hit@1 = 1.0**（不降），偏好抽取 F1 = 0.9787，全部 PASS
- 专项：`test_recall_strategy` / `test_retrieval_crosslang` / `test_metrics` /
  `test_knowledge` / `test_embedding` → **71 passed**
- 全量：**854 passed, 4 skipped**
- 词面打分等价性：3225 组零差异；向量打分：top10 顺序一致、分数差 4e-17
- 跨用户过滤、写入/版本/遗忘相关测试全部通过（未改动其语义）

## 五、达标状态与余量（如实呈现）

- **1000 条串行完整 recall P95 = 511.5ms**，距 500ms 预算**仅差 11.5ms（2.3%）**，
  较基线（1825.6ms）改善 72.0%。
- CPU 编码与并发排队是后续分项复测的重点。作者提供的 147ms 是 500 条阶段测量下
  的编码 P50。对于 1,000 条完整 recall，需用最终版本在同一链路内再确认占比。
- **4 并发 P95 = 3338.5ms**（基线 7770.3ms，-57%），仍显著超标：编码 CPU 争抢为
  结构性限制，按 Issue 要求单列。
- 500 条串行 P95 = 588ms（未达 500ms）：该样本集含更多触发词面补召的知识型查询，
  且编码 P95 波动（317ms）占主导；1k 规模的 TASK-007 harness 口径更接近验收条件。
- 复现命令见第六节；1k 复测在本机需约 25 分钟（编码 1000 条约 150s 起，机器状态相关）。

## 六、复现命令

```powershell
. .\scripts\windows_env.ps1
$env:OS_AGENT_EMBEDDING_BACKEND = "local"
$env:OS_AGENT_MEMORY_BACKEND = "memory"

# 阶段直测（对比时加 OS_AGENT_EMBEDDING_CACHE_SIZE=0 关闭缓存，保证同口径）
./.venv/Scripts/python.exe scripts/recall_stage_profile.py --scale 500 --reps 5

# 1,000 条完整 recall 与 4 并发，JSON 含每次请求耗时
./.venv/Scripts/python.exe scripts/windows_perf_acceptance.py --scale 1000 --reps 10 --workers 4 --rounds 5

# 金标不降
./.venv/Scripts/python.exe benchmarks/run_benchmarks.py --real
```

## 七、接口影响声明

- `QueryMemoryResponse` 新增 `embedding_ms` / `search_ms`（可选，默认 None）——additive
- `MemoryOrchestrator.recall()` 新增 `trace` 关键字参数（默认 None）——additive
- `InMemoryMilvusClient` 内部新增范数/矩阵缓存与环境开关——**不改外部契约**
- `LocalEmbeddingAdapter` 新增 LRU 缓存与 `cache_stats()`（观测用）——additive
- 未改动金标、计分口径与检索语义

## 八、合入复核

维护者用本机 Windows memory/hash 确认阶段探针生成了 15 个请求样本。另用主线旧实现
与合入版独立比较 3,210 组词面分数，结果一致，并核对了 10,000 条候选、租户过滤和批量删除。
本轮证据用于代码和接线验证。正式性能验收需要最终提交的 memory 与 Milvus Lite 双后端
BGE-M3 复测，附完整查询序列、配置和原始样本。
