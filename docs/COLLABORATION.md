# 协作入口与邀请说明

协作者的 AI 新对话先读 [Agent 开工指南](AGENT_ONBOARDING.md)。它汇总必读顺序、模块路径、Clerk、环境与验证方式；可直接转发文末的开工提示词。

任务工具已接入 [Clerk](CLERK.md)，状态见 [TEAM_STATUS.md](TEAM_STATUS.md)。每位协作者按指南安装后即可使用仓库中的共享配置与记录。

推荐“固定小团队 + 公开 PR”：@IweirdoI 负责路线、审查和发布；任务明确负责人及验收人。新人先 Fork，稳定成员按需要邀请为 Collaborator。普通协作者通过 PR 合入主线；所有者按明确授权可使用下文的管理员例外直接合入。

代码沿用 Apache 2.0，贡献采用 DCO。固定团队先确认 [团队协作协议](TEAM_AGREEMENT.md)，分别约定参赛、署名、费用和成果分配。

## 入组流程

1. 发仓库、贡献指南和团队协议，说明这是既有团队成果的 Windows 工程化版本。
2. 收集 GitHub 用户名、方向、每周投入预期、开发环境与是否参赛，不公开收集身份证或银行卡。
3. 填写并逐人确认协议；没有确认的经济事项不默认承诺。
4. 在 Issue 认领一个小任务，约定范围、验收和时间，开发后提 PR。
5. 固定成员需要写权限时由所有者发邀请，对方接受后推自己的分支。

可先讨论的方向：复核 Windows 安装说明；改进一个明确的前端交互；为可复现的偏好提取问题补回归；整理可分发评测案例；由具备授权与设备的成员做麒麟 SDK 集成。这些是招募方向，不代表已确认缺陷或已创建任务，认领前先查重和确定范围。

## 可复制的邀请文案

> 我在维护 MOKOA Windows 工程化版本，想组一个固定协作团队，一起完善 Agent 偏好记忆、知识检索、隐私遗忘和 Web 演示。项目基于已有团队成果继续开发，会保留原作者和上游署名。
>
> 欢迎 Python/FastAPI、Vue、测试评测、文档和麒麟环境集成方向参与。先从小任务开始，通过 Issue 认领、分支开发和 PR 审查协作。
>
> 代码使用 Apache 2.0，贡献作 DCO 签署；团队另行确认比赛成员、署名、成果和奖金安排。是否参赛、是否有偿及分配以书面确认内容为准。
>
> 仓库：https://github.com/IweirdoI/mokoa-windows
>
> 把 GitHub 用户名、想做的方向、每周大致可投入时间和可用环境发给我，我们一起选首个任务。

发出前补充双方已谈定的实际条件；新文档合入后再追加其链接。

## 发给协作者的链接

- [仓库](https://github.com/IweirdoI/mokoa-windows)
- [Windows 开发](https://github.com/IweirdoI/mokoa-windows/blob/master/docs/WINDOWS.md)
- [LangGraph 接入](https://github.com/IweirdoI/mokoa-windows/blob/master/docs/LANGGRAPH_AGENT.md)
- [任务列表](https://github.com/IweirdoI/mokoa-windows/issues)
- [新建任务](https://github.com/IweirdoI/mokoa-windows/issues/new/choose)
- [PR 列表](https://github.com/IweirdoI/mokoa-windows/pulls)
- [Fork](https://github.com/IweirdoI/mokoa-windows/fork)

协作规范：

- [新版贡献指南](https://github.com/IweirdoI/mokoa-windows/blob/master/CONTRIBUTING.md)
- [团队协议草案](https://github.com/IweirdoI/mokoa-windows/blob/master/docs/TEAM_AGREEMENT.md)
- [协作入口](https://github.com/IweirdoI/mokoa-windows/blob/master/docs/COLLABORATION.md)
- [贡献记录](https://github.com/IweirdoI/mokoa-windows/blob/master/CONTRIBUTORS.md)
- [Clerk 安装与使用](https://github.com/IweirdoI/mokoa-windows/blob/master/docs/CLERK.md)
- [共享任务状态](https://github.com/IweirdoI/mokoa-windows/blob/master/docs/TEAM_STATUS.md)

## 所有者操作入口

- [邀请协作者](https://github.com/IweirdoI/mokoa-windows/settings/access)：Add people → 输入 GitHub 用户名 → 发邀请。GitHub 为每人生成邀请；公开仓库地址不是写权限邀请链接。
- [分支保护](https://github.com/IweirdoI/mokoa-windows/settings/branches)
- [安全设置](https://github.com/IweirdoI/mokoa-windows/settings/security_analysis)

## 主线保护配置

本轮启用以下配置，是否实际生效以 GitHub 分支设置为准。后续调整应记录原因。

- 合入本次文档和模板。
- 保护 master：要求 PR、至少一位批准、Code Owner 审查、新提交使旧批准失效、解决所有审查讨论；禁止强推和删除。
- `clerk-check` 作为必需检查；业务 CI 的 test、test-windows 需观察实际运行结果，未确认稳定通过前不列为必需项。
- 初期仅 @IweirdoI 为 CODEOWNER。其本人 PR 不能自行批准，暂保留仓库所有者的管理员绕过能力，其他协作者仍须审批和检查；紧急绕过应说明原因。第二位维护者加入后再评估增加 Code Owners 并关闭管理员绕过。
- 文件提交不等于保护生效，需通过 GitHub API 或设置页核验。
- 成员加入前确认协议，退出后撤销权限与轮换凭据，保留真实贡献记录。
- 需要更多层次或模块团队权限时考虑 GitHub Organization；个人仓库 Collaborator 有写权限，不能当作目录级访问限制。

依据：[个人仓库权限](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)、[分支保护](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)、[Code Owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)。
