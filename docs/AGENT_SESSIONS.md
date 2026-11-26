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
- **遗忘联动**：REST `/forget`、聊天内遗忘路由（同步与流式）统一调用
  `SessionStore.forget_memories()`，它一次完成两件事：
  1. **抑制现存消息**——`memory_id` / `stored_memory_ids` /
     **`referenced_memory_ids`** 命中已删 id 的回合，user+assistant 双双标记
     suppressed、正文替换为占位 `[该条消息已按遗忘指令删除]`
  2. **写墓碑**——把已遗忘记忆 id 记入 `forgotten_memories` 表

  因此被遗忘文本**不会经恢复、下次请求、或晚到的回合重新送给模型**（幂等）。
  - `referenced_memory_ids` 记录**本回合回答实际引用的召回记忆**（预算筛选后的
    reference_memory），因此删除源记忆后，引用它的旧回答也会整轮退出历史。
  - **抑制不依赖 `session_id`**：前端当前仍会发送不带 session 的遗忘请求，
    同步与流式入口都在无 session 时照常扫描该用户全部已保存会话。
  - 会话回复同时记录所用偏好、记忆搜索工具和已载入历史的来源 ID。后续回答沿用了旧历史时，
    遗忘源记忆也会抑制相关后续回合。偏好记录 ID 用于会话追踪，偏好提示的格式保持原样。
- 长期记忆的召回仍走 Memory Orchestrator（respect 遗忘），会话恢复只还原展示。

## 三、恢复语义

- 仅**成功完成的回合**落库（user + assistant 双条，`status=completed`）；
  中断、超时、部分完成（未核实工具操作）与客户端断线不落库——
  恢复不会冒充成功（流式入口捕获 SSE 终态，同步入口检查 workflow.status）。
- **遗忘路由回合不落库**（`reset_history=True` 或 `route=forget`）：遗忘指令本身
  不得作为对话记录保存。
- **迟到回合拦截**：落库前用墓碑校验本回合关联的 memory id；若任一已被遗忘，
  整轮拒绝落库——覆盖「遗忘前已召回、遗忘成功后才完成」的并发时序。
  墓碑写入与现有消息抑制同事务提交，回合的墓碑校验和插入也在一个写事务内完成。
- 流式包装器必须是 **async generator**（`async for` 迭代 `agent.chat_stream`）：
  用同步 `for` 迭代异步生成器会导致 SSE 空正文且不落库。
- **流式时序**：包装器在检测到终态后**先**完成落库与遗忘抑制、**再**把
  `done` 下发——客户端收到成功终态时读历史不会看到旧内容。

## 四、Schema（PRAGMA user_version = 3）

```sql
sessions(id TEXT PK, user_id TEXT NOT NULL, title TEXT NOT NULL,
         created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
messages(id TEXT PK, session_id TEXT NOT NULL
             REFERENCES sessions(id) ON DELETE CASCADE,
         role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
         memory_id TEXT, stored_memory_ids TEXT,
         referenced_memory_ids TEXT,
         suppressed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)
forgotten_memories(user_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                   forgotten_at TEXT NOT NULL,
                   PRIMARY KEY (user_id, memory_id))
```

- v2 增量：`referenced_memory_ids`（JSON 数组）记录本回合回答引用的召回记忆 id。
- v3 增量：`forgotten_memories` 墓碑表，阻断迟到回合把旧内容写回历史。
- 旧库（v1/v2）初始化时自动 `ALTER TABLE` 补列 / `CREATE TABLE` 建表，兼容读取。

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

## 执行记录

聊天运行可以关联会话，但执行状态单独存入 `data/agent_runs.db`。
同步响应通过 `run_record_id` 返回记录 ID，SSE 响应通过 `X-Run-Record-ID` 返回。
同一用户在同步或流式入口重试同一 `Idempotency-Key` 时，收到 HTTP 409 和原始运行记录 ID、状态。
可以查询 `GET /agent/runs/{user_id}/{run_id}`，向同一路径加 `/cancel` 发起协作取消，
或调用 `POST /agent/runs/{user_id}/recover` 查询本次恢复标注的记录。
取消请求由执行该运行的进程接收。恢复候选提供已确认步骤，供后续工作流跳过。
