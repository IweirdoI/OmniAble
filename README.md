# OmniAble

**面向残障人士的无障碍就业工作台。** 工作人员选择希望从事的岗位，按自己的需要配置输入、反馈和辅助设备，再与 Agent 一起练习并完成真实工作流程。工作人员掌握业务判断和确认，Agent 协助查询、整理和操作软件，现场设备按岗位需要提供观察或实体操作能力。

**选择适配方向 → 选择工作 → 配置人员、Agent 和设备的分工 → 练习并完成工作**

## 岗位与适配方向

- **上肢操作受限** · 以定制订单、轻小物品备货为例。工作人员确认方案和处理例外，Agent 准备订单与清单，机械臂辅助取放。输入可选语音、替代鼠标或开关。
- **下肢行动受限或到场困难** · 以远程接待、展品讲解为例。工作人员远程沟通和作决定，Agent 查规格、整理记录，现场设备协助观察和展示。
- **视障** · 以业务资料核验为例。用键盘、读屏或语音逐项查看商品、数量和金额，回查来源并在提交前核对修改。
- **听障** · 以文字化的客户服务为例。工作人员查看带发言者标记的字幕，修正识别错误，确认后由 Agent 更新业务记录。
- **言语障碍** · 以咨询接待为例。工作人员使用文字或其他可用输入，审核 Agent 整理的表达，再选择播报或发文字。

适配方向帮助用户找到入口。输入方式、反馈渠道和设备由本人选择，也可以跨方向组合。

## 参赛演示

计划验证三条任务链。**动作辅助**完成一笔定制订单和实体备货，观察操作负担。**信息辅助**独立核验并提交一份业务资料，检查能否回查每一处修改。**沟通辅助**完成客户交流与业务记录，检查用户能否纠正字幕或审核表达再提交。三条演示共用项目资料、岗位知识、任务状态、Agent 工具与记忆。[产品方向与演示验收](docs/PRODUCT.md)

## 当前进度

- **已有基础** · Vue 工作台、项目资料和按版本追溯的知识卡、本地分享包、LangGraph 对话与文件工作区、短中长期记忆、偏好和多源资料接入。
- **待设计验证与集成** · 岗位配置和练习流程、各适配方向的端到端体验、机械臂真机联动和 AgentArts 云部署。相关原型和验证结果按实际环境记录。

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
