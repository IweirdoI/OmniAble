# Agent 本地会话存储设计（TASK-018 / Issue #23）

按 Issue 要求先声明数据位置、保留策略、Schema 与导入边界；实现见
`src/agent/session_store.py` 与 `api/server.py` 会话端点。

## 一、数据位置

- `data/agent_sessions.db`（SQLite 单文件，项目根相对；环境变量
  `OS_AGENT_SESSIONS_DB` 可覆盖），与 ingestion staging 同目录约定，不入 Git。
- 独立于 Memory Orchestrator 的存储（Milvus/进程内），**会话记录与长期记忆
  生命周期相互独立**——会话记录是展示与恢复用的对话副本，不是记忆。

## 二、与模型上下文的关系（遗忘联动的关键）

- **改造前**：前端每次请求自行携带 `history`（最多 20 条）→ 直接进入模型上下文。
- **改造后**：请求带 `session_id` 时由**服务端**从会话库构建 history——
  排除被遗忘（suppressed）与未完成消息；前端不再需要自行携带历史。
  不带 `session_id` 的请求行为完全不变（旧客户端兼容）。
- **遗忘联动**：/forget（REST）与聊天内遗忘路由删除记忆后，会话库把
  `memory_id` / `stored_memory_ids` 命中已删 id 的消息标记 suppressed 并将正文
  替换为占位 `[该条消息已按遗忘指令删除]`——被遗忘文本**不会经恢复或下次请求
  重新送给模型**（幂等）。
- 长期记忆的召回仍走 Memory Orchestrator（respect 遗忘），会话恢复只还原展示。

## 三、恢复语义

- 仅**成功完成的回合**落库（user + assistant 双条，`status=completed`）；
  中断、超时、部分完成（未核实工具操作）与客户端断线不落库——
  恢复不会冒充成功（流式入口捕获 SSE 终态，同步入口检查 workflow.status）。
- 聊天内遗忘路由的回合不作为对话消息记录，`deleted_ids` 转交遗忘联动。

## 四、Schema（PRAGMA user_version = 1）

```sql
sessions(id TEXT PK, user_id TEXT NOT NULL, title TEXT NOT NULL,
         created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
messages(id TEXT PK, session_id TEXT NOT NULL
             REFERENCES sessions(id) ON DELETE CASCADE,
         role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
         memory_id TEXT, stored_memory_ids TEXT,
         suppressed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)
```

## 五、API（最小集）

| 端点 | 说明 |
|---|---|
| `POST /agent/sessions` | 新建会话（user_id + 可选 title） |
| `GET /agent/sessions/{user_id}` | 分页列表（更新时间倒序，含未抑制消息数） |
| `GET /agent/sessions/{user_id}/{session_id}` | 详情 + 消息（展示口径） |
| `DELETE /agent/sessions/{user_id}/{session_id}` | 删除（级联物理删除消息） |
| `POST /agent/chat`、`POST /agent/chat/stream` | 新增可选 `session_id` 字段 |

## 六、用户隔离与并发

- 所有读写按 `user_id` 归属校验；跨用户一律 404 / None / False，不泄露存在性。
- 单连接 + 线程锁（`check_same_thread=False` + RLock）；流式落库发生在
  worker 终态之后，与请求线程共用同一把锁。

## 七、前端入口（待协调）

会话列表/切换的前端 UI 属 #11/012 的设计范围，与 UI 负责人协调后接入；
本阶段交付后端存储、最小 API 与聊天集成，前端可直接消费上述端点。
