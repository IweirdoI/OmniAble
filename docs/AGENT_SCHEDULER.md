# 本地定时任务

TASK-031 的后端随桌面宿主启动，正常关闭时由工作线程释放 SQLite 租约。任务和触发历史保存到 `data/scheduler.db`，运行记录复用宿主的 TASK-022 运行库。

`POST /agent/scheduler/tasks` 接受 `user_id`、`action`、`arguments`、`schedule_kind` 和 `schedule`。一次性任务使用 ISO 时间 `at`，间隔任务使用正整数 `every_seconds` 和可选的 ISO 时间 `start`，每日任务使用 `time` 和 IANA 时区 `tz`。时间字符串可附 UTC 偏移，省略时按宿主本地时区解释。列表、预览、暂停、恢复、删除和历史通过同一路由组提供。

无人值守只运行 `calculate` 和 `get_current_datetime`。定时计算限 64 字符并禁用幂运算。能更改状态的动作记录 `waiting_confirmation`。访问工作区、网络、记忆和插件的动作需单独接入权限后才能自动运行。

错过的旧时点只记录 `missed`，最近一次到期时点可补发。接管时未完成的触发记为 `needs_review`，已登记时点不会重发。历史只保存结果状态、运行 ID 和错误码等最小信息。

桌面状态入口、用户授权复查和插件版本绑定仍待接入。验收范围见 [Issue #39](https://github.com/IweirdoI/OmniAble/issues/39)。
