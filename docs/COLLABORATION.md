# 协作入口与邀请说明

协作者的 AI 新对话先读主线的 [Agent 开工指南](AGENT_ONBOARDING.md)，按其中的步骤刷新并读取最新设计。接手旧分支和提交前也要核对。可直接转发指南文末的开工提示词。

任务工具已接入 [Clerk](CLERK.md)，状态见 [TEAM_STATUS.md](TEAM_STATUS.md)。每位协作者按指南安装后即可使用仓库中的共享配置与记录。

推荐“固定小团队 + 公开 PR”：@IweirdoI 负责路线、审查和发布；任务明确负责人及验收人。新人先 Fork，稳定成员按需要邀请为 Collaborator。普通协作者通过 PR 合入主线；所有者按明确授权可使用下文的管理员例外直接合入。

代码沿用 Apache 2.0，贡献采用 DCO。固定团队先确认 [团队协作协议](TEAM_AGREEMENT.md)，分别约定参与、署名、费用和成果分配。

## 入组流程

1. 发 OmniAble 仓库、[产品方向](PRODUCT.md)、贡献指南和团队协议，说明适配路线与当前实现范围。
2. 收集 GitHub 用户名、方向、每周投入预期、开发环境与是否参与，不公开收集身份证或银行卡。
3. 填写并逐人确认协议；没有确认的经济事项不默认承诺。
4. 在 Issue 认领一个小任务，约定范围、验收和时间，开发后提 PR。
5. 固定成员需要写权限时由所有者发邀请，对方接受后推自己的分支。

可从键盘和读屏核验、字幕与文字沟通设计、岗位练习样例、现场设备联调或 Windows 开发验证开始。认领前先检查已有任务并确定验收范围。

## 可复制的邀请文案

> 我们在做 OmniAble，一款面向残障人士的通用无障碍 Agent 助手。用户可以在手机或电脑上交代生活、学习或工作任务，按需选择协助、分步协作或授权代办，并随时核对与接管。首轮先验证生活通知和工作资料两条任务。
>
> 欢迎无障碍设计、Vue、Python/FastAPI、辅助设备集成、测试与文档方向的伙伴。我们通过 Issue 认领、分支开发和 PR 审查协作。
>
> 代码使用 Apache 2.0，贡献作 DCO 签署；团队另行确认成员、署名、成果和奖金安排。是否参与、是否有偿及分配以书面确认内容为准。
>
> 仓库：https://github.com/IweirdoI/OmniAble
>
> 把 GitHub 用户名、想做的方向、每周大致可投入时间和可用环境发给我，我们一起选首个任务。

发出前补充双方已谈定的实际条件；新文档合入后再追加其链接。

## 发给协作者的链接

- [仓库](https://github.com/IweirdoI/OmniAble)
- [产品方向与演示](https://github.com/IweirdoI/OmniAble/blob/master/docs/PRODUCT.md)
- [当前助手设计](https://github.com/IweirdoI/OmniAble/blob/master/docs/ASSISTANT_DESIGN.md)
- [AI Agent 开工指南](https://github.com/IweirdoI/OmniAble/blob/master/docs/AGENT_ONBOARDING.md)
- [Windows 开发](https://github.com/IweirdoI/OmniAble/blob/master/docs/WINDOWS.md)
- [LangGraph 接入](https://github.com/IweirdoI/OmniAble/blob/master/docs/LANGGRAPH_AGENT.md)
- [任务列表](https://github.com/IweirdoI/OmniAble/issues)
- [新建任务](https://github.com/IweirdoI/OmniAble/issues/new/choose)
- [PR 列表](https://github.com/IweirdoI/OmniAble/pulls)
- [Fork](https://github.com/IweirdoI/OmniAble/fork)

协作规范：

- [新版贡献指南](https://github.com/IweirdoI/OmniAble/blob/master/CONTRIBUTING.md)
- [团队协议](https://github.com/IweirdoI/OmniAble/blob/master/docs/TEAM_AGREEMENT.md)
- [协作入口](https://github.com/IweirdoI/OmniAble/blob/master/docs/COLLABORATION.md)
- [贡献记录](https://github.com/IweirdoI/OmniAble/blob/master/CONTRIBUTORS.md)
- [Clerk 安装与使用](https://github.com/IweirdoI/OmniAble/blob/master/docs/CLERK.md)
- [共享任务状态](https://github.com/IweirdoI/OmniAble/blob/master/docs/TEAM_STATUS.md)

## 所有者操作入口

- [邀请协作者](https://github.com/IweirdoI/OmniAble/settings/access)：Add people → 输入 GitHub 用户名 → 发邀请。GitHub 为每人生成邀请；公开仓库地址不是写权限邀请链接。
- [分支保护](https://github.com/IweirdoI/OmniAble/settings/branches)
- [安全设置](https://github.com/IweirdoI/OmniAble/settings/security_analysis)

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

## 合并受阻排查

没有文本冲突只代表 Git 可以合并，不代表 CI、审批、未解决讨论及实际凭据的权限都已满足。
仓库所有者已授权管理员连接在验证后直接合入；普通协作者仍按现有 PR 与审查流程工作。

1. 查 PR 最新 head 与 base、`mergeable`、检查结果、审批和未解决讨论。CI 通过必须对应当前 head，不能使用旧提交结果。
2. 检查当前执行通道的身份与仓库权限，再核对 classic branch protection、rulesets 和管理员强制开关。
   不读取或打印令牌正文，也不把仓库元数据中显示的 owner/admin 字段当作集成令牌必能绕过的证明。
3. `REVIEW_REQUIRED` 表示审批门槛尚未满足；`--admin` 是申请使用当前凭据已有的管理员能力，不会给 App 或 bot 令牌增加能力。
   对话中存在管理员持续授权，也不会改变 GitHub 发放给集成的令牌权限。
4. 如果会话中另有已授权的仓库所有者 GitHub 连接，先通过该连接验证身份与权限，再以预期 head SHA 执行合并。
   成功后回读 `merged`、`merged_by` 与主线提交；不因某一条 Agent 通道失败就断言所有管理员通道都不可用。
5. 只有 bot／集成通道、没有可用管理员连接时，记录实际错误及凭据限制，再说明需要怎样的身份连接。
   不自行把所有 App 加入绕过名单，不关闭全仓审查或 CI，不使用强推绕过，也不向用户索要明文令牌。

2026-11-22 的 PR #27 是一次已核验的案例：无冲突，四项 CI 通过，审批列表为空；
Cursor bot 通道报告 `REVIEW_REQUIRED`，仓库的管理员强制开关为关闭。
随后通过仓库所有者的已授权连接成功合并，未更改主线保护。该结果说明可用的管理员通道能够合入，
不代表其他 Agent 的集成令牌因此取得同样权限。
