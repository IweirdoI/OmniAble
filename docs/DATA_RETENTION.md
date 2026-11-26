# 数据保留边界契约（TASK-034 / Issue #42）

- 执行：HXFhappiness ｜ 日期：2026-11-24 ｜ 分支：`hxf/task-034-retention-boundary`
- 机器可读事实来源：`src/security/retention.py`（`RETENTION_POLICY`）；本表与其同步维护
- 回归：`tests/test_retention_boundaries.py`（10 项，含遗忘重启、换盘迁移、脱敏占位召回）

## 一、数据类别与保留边界

按用户/工作区列出各类数据的归属、保存位置与期限。`location = 未落地` 的类别
由未来的插件/运行记录任务（TASK-022/024/029/031 等）按本契约实现，不得另立边界。

| 类别 (kind) | 归属 | 保存位置 | 期限 | 遗忘传播 | 进长期记忆 |
|---|---|---|---|---|---|
| `long_term_memory` 长期记忆（偏好/知识） | 按用户 | 向量后端（memory / milvus） | 直至遗忘 | ✅ | ✅ |
| `preference_profile` 偏好画像 | 按用户 | 向量后端投影；版本索引 `data/preferences/<user_id>.json` | 直至遗忘 | ✅ | ✅ |
| `sensitive_index` 脱敏索引（加密原文） | 按用户 | `security_index.json` | 直至遗忘 | ✅ | ❌ |
| `agent_session` Agent 会话与消息 | 按用户 | `data/agent_sessions.db` | 持久（用户可删） | ✅ | ❌ |
| `ingestion_staging` 摄取暂存审计 | 按用户 | `data/ingestion_staging.db` | 持久（遗忘时清洗） | ✅ | ❌ |
| `recording` 操作录音/录屏 | 按用户 | 未落地 | 会话级（任务结束即清理，除非用户显式保存） | ✅ | ❌ 默认不摄入 |
| `template` 流程模板 | 工作区 | 未落地 | 持久（显式保存，**与执行授权分开存放**） | ❌ | ❌ 默认不摄入 |
| `table_row` 批量录入表格行值 | 按用户 | 未落地 | 会话级（运行明细随任务清理） | ✅ | ❌ 默认不摄入 |
| `task_log` 任务执行日志 | 按用户 | `data/agent_runs.db`（可用 `OS_AGENT_RUNS_DB` 指定） | 持久（**只留最小结果**：状态/耗时/错误码及步骤或工件元数据） | ❌ | ❌ 默认不摄入 |
| `code_index` 代码解析索引 | 工作区 | 未落地 | 持久（可重建） | ❌ | ❌ 默认不摄入 |
| `execution_authorization` 执行授权记录 | 按用户 | 未落地 | 持久（独立于模板存放） | ❌ | ❌ |

**流程/插件数据（录音、模板、表格行、任务日志、代码索引）一律默认不摄入长期记忆**
（`PLUGIN_DATA_KINDS`）。现有摄入管线 `DataSource` 仅接受 conversation / document /
tool_result / user_behavior / manual_config 五类来源；未登记来源（recording 等）在
内容处理前即被 `invalid_source` 拒绝。该校验针对来源标识。现有显式知识写入支持
`repo_scan`、`agent_log` 等 `MemorySource`。未来插件接入时应在调用摄取接口前应用
保留契约，新增来源需同步评审 `retention.py` 与 `DataSource` 枚举并补回归。

## 二、遗忘传播矩阵

遗忘一条记忆（`orchestrator.forget` → API `/forget`）后必须同步清理的目标
（`FORGET_PROPAGATION_TARGETS`，全部已接线）：

1. **脱敏索引** — `security.delete_sensitive_entries`（加密原文删除）
2. **短期层** / 3. **中期层** / 4. **长期层** — `layer_manager.delete_short/mid/long`
5. **向量后端** — `delete_memory`（含 milvus 批量过滤删除）
6. **摄取暂存** — `staging.forget_memory`（事件清洗 + 原始删除）
7. **偏好版本索引** — `_sync_pref_index_delete`（画像一致）
8. **会话引用** — `session_store.forget_memories`（墓碑 + 抑制现存消息；晚到回合落库被拦）

## 三、失效引用清单

以下引用在遗忘/切换用户后失效（或按用户闭包，跨用户读不到）：

- 会话恢复（`load_model_history`）：已遗忘记忆的 id 不再出现在来源集合；
  引用它们的回合正文被抑制为占位，永不进入模型历史（TASK-018 联动）。
- 偏好画像（`get_preference_profile`）：从向量后端投影构建，遗忘后同步消失。
- 会话/消息/会话详情：严格按 `user_id` 闭包；跨用户查询返回空/None（fail-closed）。
- 脱敏占位召回：正文以 `<HASH_xxx>` 入库，本人可按主题召回但不含原始值；
  跨用户不可见。

## 四、重启与迁移语义

- 数据位置全部相对项目根/数据目录，**不内嵌盘符**；整目录迁移（换盘/换机）后
  布局不变（`test_storage_layout_survives_relocation`）。
- `InMemoryMilvusClient` 是进程内开发后端，重启即空——正式持久化验收使用
  milvus-lite / Milvus（TASK-007 口径）。测试中通过共享后端实例模拟持久层。
- 偏好版本索引按 `data/preferences/<user_id>.json` 持久化，新实例按需惰性加载。

## 五、最小日志与默认不摄入

- 任务执行日志只留最小结果（状态/耗时/错误码）；正文、表格值、录音转写、源码
  默认不写入任何长期存储（验收原文），运行明细随任务清理。
- 模板（可复用流程定义）与执行授权（谁/何时/何范围获准执行）分开存放；
  模板文件不包含授权凭据。

## 六、复现命令

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_retention_boundaries.py -q
./.venv/Scripts/python.exe -m pytest -q  # 全量
```
