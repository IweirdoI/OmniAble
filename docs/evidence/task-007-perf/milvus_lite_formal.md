# TASK-007 Windows 桌面应用目标后端性能验收

- 生成时间：2026-11-22T22:21:38 ｜ 提交：未知
- 运行类型：**正式验收**
- 环境：Windows 10.0.19044 / AMD64 / AMD64 Family 25 Model 117 Stepping 2, AuthenticAMD / RAM 31.3GB
- 配置：embedding=local（BAAI/bge-m3，1024 维，fallback=False）｜ memory=milvus
- 模型加载：12.94s ｜ 规模编码：159.61s ｜ 插入：1.97s
- 数据集：金标 14 docs / 10 queries（run_benchmarks._retrieval_corpus（内置 + knowledge_retrieval.jsonl 扩展））；规模语料 1000/1000 条

## 分阶段耗时（warm，P95 与全仓 _percentile 统一）

| 阶段 | P50(ms) | P95(ms) | 均值(ms) |
|---|---|---|---|
| 编码（embedding，直测） | 103.938 | 124.892 | 107.568 |
| 存储检索（向量检索直测） | 108.048 | 129.527 | 113.386 |
| 词面补召拉取（标量 query 直测，金标范围） | 116.873 | 131.235 | 119.968 |
| 词面补召拉取（规模 1000 条） | 261.747 | 292.765 | 264.283 |
| 重排合并与其他（估算=recall−encode−storage） | 382.893 | 374.419 | 379.886 |
| 完整 recall 全链路（金标范围） | 594.879 | 628.838 | 600.84 |
| 完整 recall 全链路（规模 1000 条） | 1722.673 | 1771.13 | 1716.481 |
| 聊天（chat） | status=failed {"http_status": 503} | | |

- **完整 recall P95（scale_user（1000 条语料）） = 1771.13ms（预算 ≤500ms，FAIL；max=1783.618ms）**，样本 10
- Recall@5 = 0.9 / Hit@1 = 0.9 （目标 ≥0.85，金标 10 条，达标判定：PASS，与 --real 同计分）
- 并发 4 线程 × 5 轮：20 ops，0.47 ops/s，P50=7994.86ms / P95=12931.006ms（max=14349.773ms），失败率 0.0
- 冷启动：模型加载 12.94s；检索链路首次 recall 646.208ms（1 条结果）

> 词面补召拉取（lexical_fetch）为直测；重排合并与其他环节为估算残差（recall_total − encode − lexical_fetch − storage）
> chat 需要可用模型服务，缺测时单列
> memory 后端为进程内接线对照；milvus 模式单独验证持久化
