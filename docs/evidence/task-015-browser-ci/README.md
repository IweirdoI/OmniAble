# TASK-015 前端浏览器回归 CI

日期：2026-11-25

## 交付结构

- `.github/workflows/ci.yml` 新增 `browser` 作业：
  - Ubuntu + Python 3.12 + Node 22；
  - 安装 `requirements-windows.txt` 中的隔离 API 依赖；
  - 使用 `frontend/package-lock.json` 安装 pinned Playwright；
  - `npx playwright install --with-deps chromium`；
  - 启动 `serve_api.py` 和 Vite 后运行浏览器回归；
  - 失败时上传 `frontend/test-results/`。
- `frontend/package.json` 固定 `playwright@1.55.0`，锁文件同步。
- `frontend/tests/browser-artifacts.cjs`：跨平台浏览器启动参数，以及失败截图/日志。
- `frontend/tests/run-browser-ci.sh`：CI 与本地 Git Bash 共用的启动、等待、清理脚本。
- `frontend/tests/chat-flow-browser.cjs`：聊天 done/error/EOF 与遗忘流程。

## 覆盖

- 聊天：正常 `done`、`error`、提前 EOF；失败不能被当成成功。
- 遗忘：请求结果展示和聊天上下文清理。
- 接入：PDF/Word、空白图片拒绝、图片说明、重复、用户切换、长期同步失败、网络失败、演示模式。
- 场景：全局/编程/办公请求、画像 `global_only`/`scene_id`、切场景清空历史。
- 移动端：390、768、键盘 focus、菜单、手机入口和无横向溢出。
- 工作台：插件、记忆面板、草稿、同步失败重试和焦点返回。

浏览器检查数量：chat-flow 4 项、workbench 22 项、ingestion 12 项，共 38 项。

## 本地复现

Windows（手动启动两个服务）：

```powershell
# 终端 1
& 'D:\ICT\.venv\Scripts\python.exe' 'frontend/tests/serve_api.py'
# 记录输出的 FIXTURE_DIR

# 终端 2
cd frontend
$env:VITE_API_TARGET='http://127.0.0.1:18086'
npm run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

然后执行：

```powershell
$env:FRONTEND_TEST_URL='http://127.0.0.1:5174'
$env:PLAYWRIGHT_MODULE='D:\ICT\tmp\dsh-playwright\node_modules\playwright-core'
$env:BROWSER_ARTIFACT_DIR='D:\ICT\.worktrees\task-015\frontend\test-results'
node frontend/tests/chat-flow-browser.cjs
node frontend/tests/workbench-browser.cjs
node frontend/tests/ingestion-browser.cjs '<FIXTURE_DIR>'
```

CI/Git Bash 可直接运行 `bash frontend/tests/run-browser-ci.sh`；脚本负责启动和清理服务。

## 失败材料

`BROWSER_ARTIFACT_DIR` 默认为 `frontend/test-results/`，包含：

- `*-failure.png`：失败页面截图；
- `*-failure.log`：失败堆栈；
- `api.log`、`vite.log`：隔离服务和预览日志。

CI 在作业失败时上传 `frontend-browser-artifacts`，保留 7 天。

## 边界

- CI 使用 Linux Chromium；本地 Windows 默认 Edge，也可通过 `PLAYWRIGHT_CHANNEL` 指定。
- `serve_api.py` 使用 memory/hash 和本地 synthetic model，不访问真实用户资料、外部 LLM 或真机 SDK。
- 浏览器测试不代表真实模型质量、真实手机键盘或麒麟原生 SDK 验收。
- 既有 `frontend` 作业继续负责 `npm test` 和 `npm run build`；本任务不重复运行 Python 全量测试。