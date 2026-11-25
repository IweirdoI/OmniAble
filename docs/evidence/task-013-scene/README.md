# TASK-013 场景选择验收

日期：2026-11-24

## 截图

- 1440 宽度，编程场景：`after-1440-scene-coding.png`
- 390 宽度，办公场景：`after-390-scene-office.png`

截图来自隔离 fixture（真实 FastAPI、memory/hash、无真实模型），只用于验证场景选择器、响应式布局和切换后的空对话状态。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py -q
# 28 passed

.\.venv\Scripts\python.exe -m pytest tests/test_core.py -q
# 通过

cd frontend
npm test
# 32 passed

npm run build
# Vite production build 通过

$env:PLAYWRIGHT_MODULE='...\playwright-core'
$env:FRONTEND_TEST_URL='http://127.0.0.1:5174'
node tests/workbench-browser.cjs
# 22 checks passed
```

浏览器回归额外验证：

- 初始请求画像带 `global_only=true`；
- 切到编程场景后，聊天请求带 `scene_id=coding`，画像请求带 `scene_id=coding`；
- 切到办公场景后，画像请求带 `scene_id=office`，旧对话和旧召回清空；
- 1440、768、390 以及 720 CSS px 的 200% 等效宽度无水平溢出。

## 边界

- 使用 Windows、Python 3.12、Node 24、无头 Edge 和隔离 fixture。
- 没有运行真实模型、真实手机键盘、麒麟原生 SDK 或生产数据库。
- 浏览器聊天步骤通过本地路由返回 SSE `done`，用于验证前端请求和清空行为，不代表真实模型质量。