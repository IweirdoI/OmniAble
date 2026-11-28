# TASK-043 订单字段核验验收

日期：2026-11-26

## 交付

- `frontend/src/order-verification.js`：字段解析、来源版本复核、数量/金额校验、重复确认防重、读屏可访问面板。
- `frontend/src/order-practice.js`：导出内置订单和客户确认单文件名，保持虚构样例单一来源。
- `frontend/src/ProjectLibrary.js`：在待核验订单详情中挂载核验面板；跨页来源版本变化时保留当前草稿并显示复核要求。
- `frontend/src/project-library.css`：核验面板样式。
- `frontend/tests/order-verification.test.js`：解析、改正和版本复核单元测试。
- `frontend/tests/project-library-browser.cjs`：键盘入口、取消、提交、来源更新、重复确认和无 POST 回归。
- `docs/ORDER_VERIFICATION.md`：交互和边界。

## 实测

```text
node --test
36 passed

node node_modules/vite/bin/vite.js build --configLoader runner
built successfully

node frontend/tests/project-library-browser.cjs
PASS project-first landing, folders, file versions and knowledge review
PASS explicitly selected share package, independent import, persistence and mobile layout
PASS lifecycle topic and lane views, independent retry and unsent chat draft
PASS keyboard entry, conflicting source fields, source link, no duplicate project or POST
```

浏览器回归实际覆盖：

- 打开模拟订单练习，焦点落在「待核验订单.md」；
- 读取来源字段和原订单字段；
- 点击取消后没有核验结果；
- 改正数量 3、金额 84；
- 通过 localStorage + StorageEvent 模拟另一标签页把客户确认单升级到 v2；
- 面板显示“来源有更新，需复核”，提交被阻止；
- 采用最新来源后提交，结果只出现一次；
- 重复点击提交后结果仍为一条；
- 全流程没有 POST 请求。

## 边界

- 这是浏览器本地虚构样例；没有真实订单、后端写入或业务提交。
- Windows NVDA 实走尚未执行；不能把 Playwright/Edge 自动化当成读屏真机验收。
- 未接入 TASK-027 的键盘/语音命令目录、TASK-042 的通用任务状态或 TASK-035 的断网真机验收。
- 没有提交密钥、真实用户数据、音频或模型。