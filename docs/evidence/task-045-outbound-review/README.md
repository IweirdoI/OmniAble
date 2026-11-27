# TASK-045 对外表达审核验收

日期：2026-11-28

## 交付

- `frontend/src/outbound-expression.js`：原始意图、草稿版本、价格/承诺确认、取消、停止播报和模拟外发状态机。
- `frontend/src/ProjectLibrary.js`：在待核验订单详情中挂载对外表达面板。
- `frontend/src/project-library.css`：面板、价格/承诺高亮、结果和状态样式。
- `frontend/tests/outbound-expression.test.js`：版本确认和可发送条件单元测试。
- `frontend/tests/project-library-browser.cjs`：取消、确认 v1、修改 v2、重新确认、停止播报和无 POST 回归。
- `docs/OUTBOUND_EXPRESSION.md`：交互、语音/真实外发边界。

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

浏览器回归检查了：

- 原始意图和草稿同时可见；
- 取消不产生结果；
- 勾选价格与发货承诺后确认 v1，模拟外发；
- 修改草稿后 v2 旧确认失效，“模拟外发”不可用；
- 重新勾选、确认 v2 后模拟外发；
- 停止播报只更新本地状态；
- 全程无 POST。

## 边界

- 没有真实 TTS、语音播报、麦克风权限、外发渠道或音频。
- 没有 TASK-028 集成；本样例是本地文字模拟，不把模拟外发当成真实发送。
- 没有 TASK-042/TASK-027 通用任务状态。
- 没有真实客户、订单、价格承诺、用户资料或网络请求。
- NVDA 真机验收未执行；浏览器自动化不能替代读屏和真实语音验收。