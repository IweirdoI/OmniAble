# Clerk 团队协作

这里接入 [invefa/Clerk](https://github.com/invefa/Clerk) 开源任务与文档管理工具的公开发行版。只使用上游工具，不开发或内置其源码；本仓保留独立配置与任务记录。

## 安装与共享

上游为公开的 Apache 2.0 项目，发行包可直接下载；协作者无需加入 TW 或取得私有仓库权限。每个人在本机安装，不共享账号或令牌。

Windows x64 协作者克隆本仓库后，在 PowerShell 执行：

```powershell
./scripts/install_clerk.ps1
./scripts/clerk.ps1 version --json
./scripts/clerk.ps1 whoami
./scripts/clerk.ps1 check
./scripts/clerk.ps1 tasks list --display
```

安装脚本从官方 [v0.1.0 Release](https://github.com/invefa/Clerk/releases/tag/v0.1.0) 下载，核对 SHA256SUMS 及安装器固定的压缩包哈希，安装到当前克隆的忽略目录 `.tools/clerk/0.1.0`，不修改全局 PATH。每人各装一次。已有 Clerk 时包装脚本也能调用本机版本；提交问题时附 `version --json`，同一版本号可能有不同构建提交。

其他平台按上游源码构建说明安装；本次未验证 Linux/macOS 可执行文件。不要把 Windows 二进制提交到仓库。

共享的是 Git 中的配置、任务表、状态页及封存记录。不会自动同步其他人的未推送工作，也不是在线多人服务。`.git/clerk/` 内的身份、锁、会话和回执属于各自克隆，不共享。Git 身份由每个人自行设置，不复制维护者身份；GitHub noreply 邮箱也会随贡献公开。

## 自动同步

- 在主仓克隆的干净 `master` 上，用 `./scripts/clerk.ps1 tasks list --display` 或 `./scripts/clerk.ps1 tasks show TASK-011` 查询时，入口脚本先抓取 `origin`，最多等待 5 秒，成功后尝试快进到 `origin/master`。网络失败、超时、分叉或工作区有改动时，查询会提示并显示本地记录。个人分支和 Fork 查询当前检出的台账。
- Clerk 写入前自动抓取并检查配置的 `origin/master`。如果已知远端有尚未包含在当前分支的提交，Clerk 会拦截写入，等分支按 Git 协作流程更新后再继续。
- 需要手动刷新干净的主仓 `master` 时，也可以明确运行 `./scripts/clerk.ps1 sync update`。

## 任务推进

先在 GitHub Issue 确定范围、负责人和验收。维护者在一条统一登记分支创建任务，并通过 PR 合入主线，其他成员从更新后的主线开工。不要在多个离线分支同时分配新编号。

```powershell
./scripts/clerk.ps1 tasks new --title "复核 Windows 上手流程" --accept "干净环境安装成功，附脱敏步骤与日志" --issue "实际 Issue URL"
./scripts/clerk.ps1 tasks list
```

用工具返回的真实编号操作，不照抄不存在的编号：

```powershell
./scripts/clerk.ps1 tasks claim TASK-001
./scripts/clerk.ps1 tasks start TASK-001
./scripts/clerk.ps1 tasks review TASK-001
./scripts/clerk.ps1 render
./scripts/clerk.ps1 check
```

`claim` 可用于 `todo`、`active`，以及 `done` 且 `claimant` 仍为空的补记（例如 PR 已合入但漏跑认领）。用交付者的 Git 姓名与邮箱执行 `claim`，不要手改 CSV，也不要把维护者验收身份写成认领者。

然后将业务改动与任务记录提交个人分支，提 PR。维护者在 PR 已合并、验收完成后记录证据与真实提交：

```powershell
./scripts/clerk.ps1 tasks complete TASK-001 --evidence "实际合并 PR URL" --commit "实际提交 SHA"
./scripts/clerk.ps1 render
./scripts/clerk.ps1 check
```

证据与提交示例必须替换为真实值；完成操作也通过后续 PR 保存。暂停用 `tasks pause <编号> --note "原因"`，恢复用 `tasks resume <编号>`；释放认领用 `tasks release <编号>`。精确字段与状态以 `help tasks`、`explain tasks` 为准。

## 合并后的验收闭环

维护者处理任务合并时，自动核对对应 Clerk 任务的验收与状态，无需等队友提醒或再次请求已有授权。
这里的“审查”指 Clerk 任务验收，不额外启动独立代码审查服务或定时任务。

1. 确认远端主线确实包含本轮交付，核对任务关联的 PR、真实合入提交及验收范围。
2. 核对实际测试和检查结果；已有且适用于最终版本的证据可以复用。区分本地、CI 和真机结果，
   缺测或待完成的检查不得写成通过。
3. 交付已完整合入、验收通过且任务处于 `review` 时，通过 `tasks complete` 写入真实
   `evidence` 和 `commit`，再执行 `render`、`check` 及仓库配置检查。保留原认领者与贡献来源。
4. 仅部分阶段合入、验收失败或缺少必需证据时，不标为 `done`，记录尚缺事项并说明实际状态。
5. 提交并同步工具生成的任务表、封存和状态页；核对远端记录已更新后再报告闭环完成。

普通协作者仍按 PR 流程提交；已核验账号与管理员权限的 Linnea 按仓库持续授权直接更新 `master`。
不要手改受管 CSV、把“PR 已合并”自动等同于“验收通过”，或遗漏验收通过后的状态登记。

## 边界与冲突

- GitHub Issue 保存讨论与验收范围；Clerk 保存同一任务的状态、认领与证据，`issue` 字段关联两者；PR 负责审查和合并。
- Clerk 声明不提供访问权限控制，最终权限由 GitHub 分支保护和 PR 审查执行。
- 本仓通过 Git 和 PR 提交共享变更。Fork 开发时按[贡献指南](../CONTRIBUTING.md)同步主仓的最新提交。
- `.clerk/data/`、`.clerk/archive/` 和状态页的生成区块只由工具维护；配置可审查修改。不要直接手改数据表或生成区块。
- 多人修改同一任务时先协调。出现 CSV、永久编号表或封存冲突，不任选一边覆盖；保留各自记录，让维护者逐项复核，执行 render、check；确需人工修复后才能以有说明的 seal 重新封存。
- 任务状态页见 [TEAM_STATUS.md](TEAM_STATUS.md)。`done` 需要经过 review 并附证据及提交，不能把“提了 PR”写成“已合并”。
- 不启用 TW 的 audit 钩子或特定词表规则；本项目继续使用自己的贡献及 DCO 规范。
- CI 安装同一公开发行版并执行 `clerk check`，另用 `python scripts/check_clerk_config.py` 检查本仓约束，不需要私有下载凭据。
