# TASK-007 Windows 桌面应用目标后端性能验收

- 生成时间：2026-11-22T22:12:59 ｜ 提交：未知
- 运行类型：**接线干跑**（fallback=hash/stub：数字仅验证链路，不给正式判定）
- 环境：Windows 10.0.19044 / AMD64 / AMD64 Family 25 Model 117 Stepping 2, AuthenticAMD / RAM 31.3GB
- 配置：embedding=hash（BAAI/bge-m3，1024 维，fallback=True）｜ memory=memory
- 模型加载：0.0s ｜ 规模编码：0.09s ｜ 插入：0.01s
- 数据集：金标 14 docs / 10 queries（run_benchmarks._retrieval_corpus（内置 + knowledge_retrieval.jsonl 扩展））；规模语料 300/300 条

## 分阶段耗时（warm，P95 与全仓 _percentile 统一）

| 阶段 | P50(ms) | P95(ms) | 均值(ms) |
|---|---|---|---|
| 编码（embedding，直测） | 0.124 | 0.149 | 0.131 |
| 存储检索（向量检索直测） | 2.031 | 2.107 | 2.059 |
| 词面补召拉取（标量 query 直测，金标范围） | 0.089 | 0.131 | 0.104 |
| 词面补召拉取（规模 300 条） | 0.202 | 0.587 | 0.343 |
| 重排合并与其他（估算=recall−encode−storage） | 6.925 | 11.592 | 8.014 |
| 完整 recall 全链路（金标范围） | 9.08 | 13.848 | 10.204 |
| 完整 recall 全链路（规模 300 条） | 329.695 | 330.791 | 325.787 |
| 聊天（chat） | status=failed {"http_status": 503} | | |

- **完整 recall P95（scale_user（300 条语料）） = 330.791ms（预算 ≤500ms，接线干跑（无正式判定）；max=330.913ms）**，样本 3
- Recall@5 = 0.9 / Hit@1 = 0.9 （目标 ≥0.85，金标 10 条，达标判定：接线干跑（无正式判定），与 --real 同计分）
- 并发 2 线程 × 2 轮：4 ops，2.73 ops/s，P50=690.496ms / P95=838.077ms（max=863.973ms），失败率 0.0
- 冷启动：模型加载 0.0s；检索链路首次 recall 16.334ms（1 条结果）

> 词面补召拉取（lexical_fetch）为直测；重排合并与其他环节为估算残差（recall_total − encode − lexical_fetch − storage）
> chat 需要可用模型服务，缺测时单列
> memory 后端为进程内接线对照；milvus 模式单独验证持久化
> 本报告为接线干跑：fallback 状态在首次实际编码后确认，不给出正式验收判定；正式判定请以 local 真实编码运行的报告为准。
