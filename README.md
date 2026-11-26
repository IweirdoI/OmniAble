# OmniAble

**面向残障人士的通用无障碍 Agent 助手。** 当手机或电脑是处理生活事务、学习或工作的工具，用户可以直接交代想做的事。Agent 可以解释屏幕内容、协助操作，也可以在授权范围内代办一部分任务。用户决定交给它哪些步骤，随时可以查看进度、补充信息、暂停或接管。

**告诉助手要做什么 → 选择协助方式和范围 → 一起处理或交给 Agent → 核对结果**

## 可以用在哪些地方

- **生活** · 从一张通知中找出办理要求，整理家庭日程，准备预约或消息草稿。
- **学习** · 读取资料并定位来源，把内容整理成可听、可读的摘要，准备待核对的笔记。
- **工作** · 查询资料、填写表格、比较文档、组织回复，或在确认后代为执行受支持的操作。重复任务可以选用模板。

语音、键盘、开关、触屏、字幕和读屏等能力按本人需要选择，可以跨适配方向组合。Agent 能操作的范围取决于当前设备、可接入的应用及用户授权。外发、提交等会产生外部影响的动作需要先核对，再由用户确认。

## 参赛演示

计划用定制备货、资料核验、客户沟通验证动作、信息和沟通辅助。另用整理生活通知验证同一套委托机制能处理临时任务。岗位模板和现场设备按场景接入。[产品方向与演示验收](docs/PRODUCT.md)

## 当前进度

- **已有基础** · Vue 工作台、项目资料和按版本追溯的知识卡、本地分享包、LangGraph 对话与文件工作区、短中长期记忆、偏好和多源资料接入。
- **待设计验证与集成** · 通用任务委托和接管、手机端任务确认、端到端辅助技术体验、机械臂真机联动和 AgentArts 云部署。相关原型和验证结果按实际环境记录。

技术主线是 Vue → FastAPI → LangGraph。Agent 通过既有 Memory Orchestrator 处理记忆召回、摄取与遗忘。Windows 是当前本地开发环境。开发使用进程内存储和 hash 向量，完整环境按需配置语义模型和持久化后端。[记忆架构](docs/ARCHITECTURE.md) · [Agent 工作流](docs/AGENT_WORKFLOW.md) · [AgentArts 适配方案](docs/designs/DOC-001-agentarts-adaptation.md)

## 本地启动

在 Windows 安装 Python 3.12 和 Node.js 22，从仓库根目录启动 API：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts/dev_windows.ps1 -Port 18080
```

另开终端运行前端：

```powershell
cd frontend
npm ci
npm run dev
```

打开 [本地工作台](http://localhost:5173/)，首页为「项目资料」。实时对话需要另配可用模型服务。[Windows 开发](docs/WINDOWS.md) · [对话接入](docs/LANGGRAPH_AGENT.md) · [前端指南](frontend/README.md)

## 团队协作与许可

- 新成员阅读[贡献指南](CONTRIBUTING.md)和[团队协作协议](docs/TEAM_AGREEMENT.md)，在 [Issues](https://github.com/IweirdoI/OmniAble/issues) 认领任务。给协作者的链接和邀请说明收在[协作入口](docs/COLLABORATION.md)。
- AI Agent 从 [AGENTS.md](AGENTS.md) 和 [Agent 开工指南](docs/AGENT_ONBOARDING.md) 开始。任务使用 [Clerk](docs/CLERK.md) 管理，实际认领见[团队任务状态](docs/TEAM_STATUS.md)。
- 代码采用 [Apache 2.0](LICENSE)，新增贡献按 [DCO](DCO) 签署。原有技术基础与贡献来源见[发布说明](docs/发布说明.md)和[贡献记录](CONTRIBUTORS.md)。
