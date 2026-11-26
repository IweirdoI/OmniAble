# 贡献指南

使用 AI Agent 的协作者，新对话先读 [AGENTS.md](AGENTS.md) 与 [Agent 开工指南](docs/AGENT_ONBOARDING.md)，再按本指南执行贡献流程。

适用于 OmniAble 的新贡献。岗位与适配方向见 [产品说明](docs/PRODUCT.md)，模块入口见 [Agent 开工指南](docs/AGENT_ONBOARDING.md)。固定团队成员以可追溯行为确认 [团队协议](docs/TEAM_AGREEMENT.md)。

## 认领与开发

固定团队使用 [Clerk](docs/CLERK.md) 记录任务认领与状态，并关联 GitHub Issue。任务与受管记录变更提交前运行 `./scripts/clerk.ps1 check`；各人自行安装工具，不共享本机身份或凭据。

1. 先阅读 [项目说明](README.md)、[发布说明](docs/发布说明.md)、[Windows 开发](docs/WINDOWS.md)。
2. 在 [Issues](https://github.com/IweirdoI/OmniAble/issues) 认领或新建任务，约定负责人、目录、交付、验收人、时间和依赖，由维护者确认。
3. 一个任务一个分支、一个 PR；较大改动先讨论接口。文档、测试、设计、评测和审查也是贡献。
4. PR 目标为 `master`，维护者审查后合并；不直接推主线，不强推共享分支，不用旧分支整份文件覆盖主线。

以上是普通协作者的默认流程。仓库所有者在明确授权且实际具备管理员权限时可以直接合入，仍需验证、签署和可追溯提交；这项例外不自动授予其他协作者。

新人优先 Fork，不需要写权限。Fork 后在自己的克隆中执行：

```bash
git clone https://github.com/YOUR_GITHUB_LOGIN/OmniAble.git
cd OmniAble
git remote add upstream https://github.com/IweirdoI/OmniAble.git
git fetch upstream
git switch -c feat/123-short-description upstream/master
```

固定成员获邀后可克隆主仓库，以 `origin/master` 为起点创建个人分支。开始前检查 `git status`，妥善保留自己的未提交改动，再更新远端。

```bash
git add <本次任务的文件>
git commit -s -m "fix(preference): 修复偏好版本更新"
git push -u origin HEAD
```

Fork 的 `origin` 是自己的仓库。在 GitHub 创建 PR，base 选择 `IweirdoI/OmniAble:master`。

只改任务约定范围。公共接口、`api/server.py`、`src/core/`、配置、依赖变更先说明影响。任务认领以 Issue 和 Clerk 记录为准。CODEOWNERS 用于请求审查，主线保护见[协作说明](docs/COLLABORATION.md#主线保护配置)。维护者为 [@IweirdoI](https://github.com/IweirdoI)。

## 接口与环境

- 契约先看 `src/common/interfaces.py`、`src/knowledge/memory_service/memory_schema.py` 和现有调用链。
- Embedding 统一入口为 `src/embedding/embedding_interface.py`，不要绕过统一接口另建存储链路。
- Windows 开发使用进程内 memory，持久化可显式选择 Milvus。
- 分开记录 Windows memory/hash 接线、BGE 语义检索及持久化后端证据，说明机器、后端和模型。

## 验证与提交

Windows 需要 Python 3.12，安装步骤见 Windows 开发文档：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1
. ./scripts/windows_env.ps1
./.venv/Scripts/python.exe -m pytest tests/test_preference_extract.py tests/test_preference_orchestrator_wire.py tests/test_api.py::test_health -q
```

代码变动运行相关测试，按影响范围运行 `python -m pytest tests/ -q`。仅文档变更检查链接与步骤。前端在 `frontend/` 安装依赖后执行 `npm run build`，界面变化附截图。不能运行的检查注明原因，不写成通过。

禁止提交密钥、`.env`、聊天原文、个人资料、本地数据库、模型、缓存和构建产物。使用虚构、脱敏且允许再分发的测试材料。密钥由各自环境变量或仓库 Secrets 配置。

使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`。`CHANGELOG.md` 由 git-cliff 生成。填写 PR 模板，说明行为、验证结果、兼容性、参与者和第三方来源。

## DCO 与许可

所有新增贡献提交用 `git commit -s` 确认 [DCO 1.1](DCO)。这是来源声明，和密码学签名不同，不要求 GPG。签署前阅读原文；身份和邮箱随公开提交长期保存，可使用关联账户的 GitHub noreply 邮箱，信息需能真实代表提交者。

漏签时，仅对自己的未共享最新提交可使用 `git commit --amend --no-edit -s`；已推送分支先联系维护者，不强推共享历史。合并前由维护者人工核查 DCO 和来源，目前没有自动 DCO 检查。合并推荐 Squash，应保留实际作者、共同作者 `Co-authored-by` 与有效签署记录，不替别人签署。

项目沿用 [Apache License 2.0](LICENSE)，除另有明确标注和合法约定外，提交用于合入的贡献按此许可提供。贡献者保留自身著作权，不要求转让给维护者。第三方代码、SDK、模型和数据集各自的许可仍需遵守。

引入第三方材料时写明原链接、版本、许可、改动与所需声明。AI 辅助内容也要人工验证，PR 中说明实质使用范围。不得提交无权公开的学校、雇主、原团队或企业资料。

代码贡献记录见 [CONTRIBUTORS.md](CONTRIBUTORS.md)。成果署名、论文作者顺序、报酬和分配按团队另行确认的协议执行；合并 PR 不自动取得名额、报酬或分成。行为规范见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)，安全问题见 [SECURITY.md](SECURITY.md)。
