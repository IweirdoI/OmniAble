# 通用任务存储与执行门控（TASK-042 / Issue #62）

DOC-002《通用任务委托与无障碍协作设计》的首轮落地：任务实体、状态机、
执行门控与运行记录关联。前端任务入口待 #77/#78（会话 UI / 桌面命令）
合入后二轮接入，本轮交付后端全量与两类样例流。

## 一、数据位置

- SQLite 单文件 `data/agent_tasks.db`（环境变量 `OS_AGENT_TASKS_DB` 覆盖），
  与 `agent_sessions.db`（TASK-018）、`agent_runs.db`（TASK-022）、
  调度任务库（TASK-031）并列；`tasks` + `task_steps` 两表，外键级联删除。
- 保留策略：任务及其步骤随用户删除语义后续由 DATA_RETENTION 统一收口；
  本轮不做自动清理。草稿为任务内 JSON 字段，不进长期记忆
  （复用模板无需把操作过程录成长期记忆，对齐路线图）。

## 二、状态机

任务：`ready → running ⇄ waiting_user → ready | completed`；
任意非终态 `→ suspended`（暂停），`resume` → `waiting_user`（不自动执行）；
`takeover` → `waiting_user` + 协助程度切 `assist`；重启遗留 running →
`suspended`（明细"重启后中断"）。

步骤：`pending / running / done / failed / unknown / skipped / blocked`。
- `unknown`（结果未知）：**禁止自动重发**，须经 `review` 核对回
  `pending` 后由用户显式重试；
- `failed`：任务停 `waiting_user`（异常停下，不冒充成功）；
- 外发/正式提交类步骤标 `external`：执行前必须单独确认，确认绑定参数
  指纹（SHA-256 前 16 位）；**内容变化即作废确认**（重放不能继承上次批准）。

## 三、执行门控（TaskRunner）

按序检查，任一不满足即 409 停 `waiting_user`，不启动被拦截动作：
1. 任务暂停/终态（store 守卫）；
2. `assist` 模式：不执行，返回计划提示文本（提示协助口径）；
3. 步骤 `unknown`：要求先核对；
4. 步骤非 `pending`：不能执行；
5. `external` 未确认或确认已过期。

执行通道：目录动作（TASK-021 `dispatch_action`，调度器同款权限口径——
开关未开/变更类动作明确拒绝）或 `local:*` 确定性本地步骤（纯本地计算/
留痕，无外部业务写入）。每次执行经 RunStore 记录（route=task，幂等键
`task:{task_id}:{step_id}:{step.updated_at}`——核对重试产生新键），
`run_record_id` 回填步骤：**任务与 Agent 运行的关联字段**（DOC-002
待确认项的首轮定义）。

## 四、API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /agent/tasks | 创建（kind 样例模板生成计划；mode=assist/step/delegate） |
| GET | /agent/tasks/{user_id} | 分页列表（步骤状态计数） |
| GET | /agent/tasks/{user_id}/{task_id} | 详情（计划/草稿/授权范围/步骤） |
| POST | …/draft | 补充信息/编辑草稿 |
| POST | …/suspend、…/resume、…/takeover | 暂停 / 恢复（不自动执行）/ 接管 |
| POST | …/steps/{id}/run | 显式执行一个步骤 |
| POST | …/steps/{id}/confirm | 外发单独确认 |
| POST | …/steps/{id}/review | 核对 unknown 步骤 |
| POST | /agent/tasks/{user_id}/recover | 重启恢复（遗留 running → suspended/unknown） |

## 五、样例流（同一机制）

- `life_schedule`（虚构生活通知→日程草稿）：collect（get_current_datetime）
  → organize（local:schedule_draft）→ push（external，本地留痕，未接外部系统）；
- `work_verify`（工作资料字段核验，复用 043 的来源/草稿/核对模式）：
  parse（local:parse_fields）→ verify（local:verify_fields）→
  submit（external）。

三类协助程度语义：`assist` 只提示不执行；`step` 每步由用户显式触发；
`delegate` 连续执行但 external 仍需单独确认、unknown 仍须核对。

同一任务一次只能领取一个步骤。步骤执行中再次请求会返回冲突，
重复请求已完成的步骤会返回已有结果。暂停或接管后，已经开始的步骤可以记下结果，
任务保持用户选择的状态与草稿。恢复时标记为结果未知的执行批次无法回写后续重试。
