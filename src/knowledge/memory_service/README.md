# 记忆存储 CRUD + RAG — knowledge

> **当前状态（2026-08-06）：** 四个 CRUD 函数和 Orchestrator/API 接线已存在，但
> `MilvusClient` 仍是进程内字典 mock，`search()` 只做精确字段过滤，不做向量相似度
> 排序；本地 embedding 也仍返回零向量。以下 Milvus 能力是生产目标，不代表已经完成
> 真实库验收。

本目录定义 **最终记忆库**（Milvus 向量 + 元数据）的四个接口：

| 文件 | 接口 | 说明 |
|------|------|------|
| `create_memory.py` | 存 | 校验 → 评价 → embed → Milvus |
| `query_memory.py` | 查 | id 查 / filter 已可 mock 联调；向量检索待真实 Milvus |
| `update_memory.py` | 改 | 更新 + 重新 embedding |
| `delete_memory.py` | 删 | 软删 / 物理删 |

## 数据流

```
ingestion ingestion     →  NormalizedRecord（SQLite staging 审计，可按需导出 JSONL）
preference preference      →  MemoryEntry (memory_type=preference)  ─┐
Orchestrator/其他    →  MemoryEntry (tool_call/document/...)  ─┼→ create_memory → Milvus
security security      →  解析目标 memory_id → delete_memory      ─┘
```

## 与 SQLite 的关系

- **knowledge模块：不用 SQLite**，生产最终库目标 = **Milvus**；当前后端是进程内 mock
- **摄取中转库**：ingestion SQLite staging（原始/候选审计，非最终库；可按需导出 JSONL）
- **preference不直接写 Milvus**，只产出候选 JSON 调 `create_memory`

## Embedding

`embedding_client.py` 调用core `src/embedding/embedding_interface.py`。Windows 本地使用 BGE-M3，开发接线使用确定性 hash。模型及存储配置见 [Windows 开发](../../../docs/WINDOWS.md)。

## 开发

```bash
# 当前 mock 无需 pymilvus；接入真实 Milvus 后再安装并补连接配置
pip install pymilvus
python -c "from src.knowledge.memory_service.create_memory import create_memory; ..."
```
