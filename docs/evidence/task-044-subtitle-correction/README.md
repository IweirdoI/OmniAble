# TASK-044 字幕纠错验收

日期：2026-11-27

## 交付

- `frontend/src/subtitle-correction.js`：原转写、业务原值、更正输入、确认/取消、文字回退、更正记录和本地状态。
- `frontend/src/ProjectLibrary.js`：在待核验订单详情中挂载字幕纠错样例。
- `frontend/src/project-library.css`：字幕面板样式。
- `frontend/tests/subtitle-correction.test.js`：更正差异、应用和原值不变单元测试。
- `frontend/tests/project-library-browser.cjs`：取消、文字回退、确认写入和无 POST 回归。
- `docs/SUBTITLE_CORRECTION.md`：交互、音频授权与保留设计、边界。

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

- 原转写 `蓝色收纳袋`、`2 件` 与确认单 `蓝色收纳盒`、`3 件` 的差异；
- 确认前业务字段仍为原值；
- 取消后业务字段不变；
- 点击“无麦克风？切换到文字输入”出现可编辑转写；
- 确认后业务字段变为 `蓝色收纳盒` / `3 件`；
- 更正记录只有一条；
- 页面没有 POST。

## 音频与边界

- 本样例没有录音、没有麦克风权限、没有音频上传或保存；只使用合成文字。
- “录音授权、默认不落盘、删除时清除转写/音频/模型上下文”的完整实现属于 TASK-028 接入范围，本 PR 仅记录设计边界。
- 没有实现真实 ASR/TTS、TASK-027 辅助命令、TASK-042 通用任务状态或真实业务提交。
- 没有真实用户音频、订单、密钥或模型。
- NVDA 真机实走未执行；浏览器自动化不能替代读屏验收。