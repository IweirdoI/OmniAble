# 团队任务状态

由 Clerk 从仓库中的任务表生成。具体工作范围与讨论见任务关联的 GitHub Issue；审查和合并以 GitHub PR 为准。使用方法见 [Clerk 协作指南](CLERK.md)。

<!-- clerk:begin tasks -->

| 编号 | 任务 | 状态 | 认领者 | Issue | 更新 |
|---|---|---|---|---|---|
| TASK-001 | 确认团队成员与协作规则 | 已完成 | Linnea |  | 20261122-155219 |
| TASK-002 | 复核 Windows 新人上手流程 | 已完成 | HXFhappiness |  | 20261122-152811 |
| TASK-003 | 确认署名与成果分配附件 | 已完成 | Linnea |  | 20261122-154445 |
| TASK-004 | [P1] 固定 benchmark 哈希映射，消除跨进程跑分漂移 | 已完成 | HXFhappiness | https://github.com/IweirdoI/mokoa-windows/issues/3 | 20261122-130750 |
| TASK-005 | [P1] 修正 real 评测环境标记、缺测处理和偏好指标命名 | 已完成 | HXFhappiness | https://github.com/IweirdoI/mokoa-windows/issues/4 | 20261122-151532 |
| TASK-006 | [P1] 优化统计与偏好画像全量读取，建立规模和并发基线 | 已完成 | HXFhappiness | https://github.com/IweirdoI/mokoa-windows/issues/5 | 20261122-144907 |
| TASK-007 | [P1] Windows 桌面应用目标后端检索与并发性能验收，按瓶颈优化 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/6 | 20261122-183535 |
| TASK-008 | [P1] 约束 SSE 后台线程生命周期、取消与并发资源 | 已完成 | Linnea | https://github.com/IweirdoI/mokoa-windows/issues/7 | 20261122-151536 |
| TASK-009 | [P1] 修复 SSE 提前 EOF、部分失败及最终回答不一致 | 已完成 | Linnea | https://github.com/IweirdoI/mokoa-windows/issues/8 | 20261122-151543 |
| TASK-010 | [P1] 显示记忆面板部分刷新失败，避免旧数据冒充已同步 | 已完成 | Linnea | https://github.com/IweirdoI/mokoa-windows/issues/9 | 20261122-185731 |
| TASK-011 | [P1] 确定聊天与记忆工作台视觉规范和关键页面方案 | 已完成 | 123456s-ai | https://github.com/IweirdoI/mokoa-windows/issues/10 | 20261122-203727 |
| TASK-012 | [P1] 落地工作台 UI 改版，恢复手机操作并完善键盘可用性 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/11 | 20261122-202114 |
| TASK-013 | [P2] 贯通聊天、偏好画像和记忆查询的场景选择 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/12 | 20261121-215647 |
| TASK-014 | [P2] 增加可分页的长期记忆浏览与筛选，独立于本轮聊天召回 | 进行中 | HXFhappiness | https://github.com/IweirdoI/mokoa-windows/issues/13 | 20261122-161744 |
| TASK-015 | [P1] 建立前端自动化回归 CI，覆盖聊天流式与响应式主流程 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/14 | 20261122-185731 |
| TASK-016 | [P1] 按设计完善 Agent 路由、执行预算与结果核验 | 已完成 | Linnea | https://github.com/IweirdoI/mokoa-windows/issues/16 | 20261122-151543 |
| TASK-017 | [P1] 集成 Agent 文件工作区、变更预览与网页侧栏 | 已完成 | Linnea | https://github.com/IweirdoI/mokoa-windows/issues/19 | 20261122-151544 |
| TASK-018 | [P1] Windows Agent 本地会话列表与重启恢复 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/23 | 20261122-185522 |
| TASK-019 | [P1] Agent 上下文预算与长会话截断 | 已完成 | Linnea | https://github.com/IweirdoI/mokoa-windows/issues/24 | 20261122-193044 |
| TASK-020 | [P1] Agent 文件变更的逐次用户确认 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/25 | 20261122-185525 |
| TASK-021 | [P1] 统一 Agent 动作目录与调用入口 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/29 | 20261122-204434 |
| TASK-022 | [P1] 持久化执行记录、取消与中断恢复 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/30 | 20261122-204438 |
| TASK-023 | [P1] 制定可离线验证的插件包格式与版本契约 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/31 | 20261122-204441 |
| TASK-024 | [P1] 实现受限插件宿主与常用内置工具迁移 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/32 | 20261122-204443 |
| TASK-025 | [P1] 建立 Windows 断网运行配置与本地模型降级 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/33 | 20261122-204445 |
| TASK-026 | [P1] 提供免安装的 Windows U 盘发行包 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/34 | 20261122-204450 |
| TASK-027 | [P1] 建立可被语音、键盘和读屏调用的桌面命令 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/35 | 20261122-204452 |
| TASK-028 | [P1] 实现普通话离线语音操控与可打断反馈 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/36 | 20261122-204455 |
| TASK-029 | [P1] 录制一次操作并生成可编辑的参数化流程 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/37 | 20261122-204459 |
| TASK-030 | [P1] 按模板批量录入并核对每行提交结果 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/38 | 20261122-204503 |
| TASK-031 | [P1] 实现本地定时任务与错过执行的处理 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/39 | 20261122-204505 |
| TASK-032 | [P2] 实现只读代码解析插件与可追溯依赖图 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/40 | 20261122-204508 |
| TASK-033 | [P2] 把 AI 代码解释与依赖图联动到源码 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/41 | 20261122-204511 |
| TASK-034 | [P1] 划清流程和插件数据与长期记忆的保留边界 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/42 | 20261122-204513 |
| TASK-035 | [P1] 验收断网桌面与无障碍端到端使用场景 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/43 | 20261122-204516 |
| TASK-036 | [P2] 将录制和回放扩展到获准的 Windows 应用 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/44 | 20261122-204519 |
| TASK-037 | [P2] 验证本地 MCP 工具能否复用插件权限 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/45 | 20261122-204521 |
| TASK-038 | [P1] 优化 Windows recall 词面补召与 CPU 编码并发 | 待开始 |  | https://github.com/IweirdoI/mokoa-windows/issues/47 | 20261122-215313 |

<!-- clerk:end tasks -->