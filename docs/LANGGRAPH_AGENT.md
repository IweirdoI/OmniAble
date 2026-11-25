# LangGraph 单智能体接入

当前入口：现有 Vue 聊天页 → `POST /agent/chat` → LangGraph → 现有 MemoryOrchestrator。
核心实现为 `src/agent/service.py`。LangGraph 版本固定为 1.2.11。

流程：固定召回相关记忆及偏好画像 → 模型回答（可选最多两轮补充检索）→ 摄取用户输入。
只摄取用户输入，不把模型生成内容当作事实存储。知识摄取后调用现有 consolidate，晋升仍由原有策略决定。
记忆写入失败会保留模型回答，并单独返回警告；模型失败则返回 HTTP 503，且不写入本轮内容。

## Windows 启动

本次 `.venv` 使用 `--system-site-packages` 复用本机 Anaconda 已有依赖；新增依赖安装在项目虚拟环境。
新机器建议使用独立虚拟环境，Python 3.12：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn api.server:app --host 127.0.0.1 --port 18080
```

另开终端：

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 http://localhost:5173 ，连接 API 后聊天。默认 Vite 代理目标为 18080。

## 模型配置

默认读取 `configs/default.yaml` 的 `llm` 配置，本机 LM Studio 的根地址自动补 `/v1`。
需要先启动 LM Studio 的开发者服务，并加载配置中的模型。
也可在启动后端的同一终端设置兼容 Chat Completions 的模型端点：

```powershell
$env:AGENT_BASE_URL = 'https://api.siliconflow.cn/v1'
$env:AGENT_MODEL = '填写账号可用的模型名称'
$env:AGENT_API_KEY = '填写密钥'
$env:AGENT_TIMEOUT_SECONDS = '60'
```

不要把真实密钥提交到仓库。AGENT_* 只影响聊天模型；偏好提取、语义脱敏模型仍使用原有配置。
只有确认模型支持 function calling 时才设置 `$env:AGENT_ENABLE_TOOLS = '1'`。
默认关闭可选工具，固定记忆检索始终执行，避免本地小模型不支持工具调用而无法聊天。
基础工具随 AGENT_ENABLE_TOOLS 开启，包括记忆检索、受限目录内的文件读取/搜索/写入及计算。终端命令和联网抓取另需 AGENT_ENABLE_EXEC_TOOL、AGENT_ENABLE_WEB_TOOL 显式开启；终端还要求 AGENT_EXEC_ALLOWED_COMMANDS 精确命令许可，不等于桌面操作能力。

文件读取、写入、编辑和可恢复删除可与侧栏联动；侧栏启用、版本冲突、恢复记录和网页嵌入边界见 [Agent 文件与网页工作区](AGENT_WORKSPACE.md)。工作区 API 可在不启用模型工具时单独开启。

## 接口与评测

请求示例：

```json
{"user_id":"demo_user","message":"根据我的偏好给出建议","history":[],"use_memory":true,"save_memory":true,"top_k":5}
```

返回 `answer`、`memories`、`recall_ms`、`latency_ms`、`memory_write`、`warnings`、`reset_history`。
`recall_ms` 包含记忆检索和画像准备，`latency_ms` 为总耗时，不应用总耗时判断检索 500ms 指标。
`history` 最多 20 条，仅接受 user/assistant；用户身份仍沿用当前项目本地演示的 user_id 约定，不是身份认证。

演示步骤：

1. 开启两个记忆开关，输入明确偏好，检查右侧实际入库状态。
2. 点击“新对话”，保留记忆开关，观察跨会话回答。
3. 再次新对话，关闭“使用记忆”和“保存本轮内容”，使用同样问题作对照。
4. 通过遗忘面板或“忘掉…”指令删除偏好，再开新对话验证。

关闭“使用记忆”只关闭检索，保存由单独开关控制。比较时务必新建对话，旧会话本身可能包含偏好。
会话历史仅驻留浏览器内存，不使用 LangGraph checkpoint，避免建立第二份服务端记忆档案。
聊天遗忘指令和面板遗忘都会清空前端历史；其他 API 客户端必须遵守 reset_history，并在调用 /forget 后丢弃旧 history。
记忆面板分别显示统计、偏好历史和当前画像的同步状态及最后成功时间。失败项可单独重试。
确认遗忘后清除本地快照，刷新失败时显示暂未取得数据。画像达到读取上限时显示截断提示。
输入和模型上下文走现有无状态正则脱敏；这不是覆盖所有隐式隐私的保证。

## Windows 模型与存储

本地语义检索和可选 Milvus 持久化的配置见 [Windows 开发指南](WINDOWS.md)。

## 验证命令

```powershell
$env:OS_AGENT_EMBEDDING_BACKEND = 'hash' # 仅接口测试，避免下载模型；不代表语义检索效果
.venv\Scripts\python.exe -m pytest tests/test_agent.py tests/test_api.py tests/test_api_validation.py -q
cd frontend
npm run build
```

自动测试使用真实 LangGraph、替身模型与隔离记忆边界，覆盖工具循环、用户隔离、禁用记忆、失败行为、遗忘及接口校验。
真实模型端到端验收需要运行中的模型服务。

## 有界工作流与执行状态

实现约束见 [Agent 工作流实施设计](AGENT_WORKFLOW.md)。ChatRequest 保持原字段，新增响应 workflow 对象包含 run_id、route、status、stop_reason、steps、artifacts 和 budget_used；客户端不能通过请求提升预算。普通问答保留固定记忆召回，明确遗忘与需澄清请求不调用生成模型；否定、引用和混合遗忘请求不自动删除。

configs/default.yaml 的 agent.workflow 控制工具轮次、每轮/总调用数、连续失败、deadline、有界事件队列及每进程流式并发。默认2轮工具、每轮4次、累计8次、连续失败2次、总180秒、队列64、并发4。超过并发在SSE头发出前返回429和Retry-After；同步执行超时返回504。资源初值不代表真机性能。记忆召回和画像仍串行，存储Schema和依赖不变。

SSE 保留旧事件，新增 run_started、stage、run_id、seq；终态唯一。done.answer 是最终答案，前端不能把工具轮次的中间文本拼接为最终结果。工具按call_id关联真实成功标志；工具失败、预算耗尽或缺少执行证据会给出partial并跳过本轮自动记忆保存。只有原始用户输入进入现有记忆链路，回答状态与memory_write状态独立。

停止按钮中断连接并显示“已请求停止”。取消前尚未开始的记忆写入不会启动；已经开始的操作可能完成，需同步确认，不能声称已撤销。同步调用无法强制安全杀线程，未退出的worker继续占并发额度，直至实际退出；事件循环不join等待该线程。无服务端聊天checkpoint或任务数据库。

工作流回归：后端增加 tests/test_agent_workflow.py；前端执行 node --test tests/chat-stream.test.js tests/ingestion.test.js 及 npm run build。浏览器手动验证可启动 frontend/tests/serve_workflow_api.py，在frontend终端设VITE_API_TARGET=http://127.0.0.1:18088后运行Vite。这是隔离存储、memory/hash和替身模型夹具，提供“读取”“失败”“慢速”场景，不访问外部模型服务。

## 模型上下文预算

[TASK-019](https://github.com/IweirdoI/OmniAble/issues/24) 在每次模型调用前统一检查最终消息和工具定义。`agent.workflow.context_window_tokens` 默认 32,768，固定预留 2,000 输出 token。输入以 UTF-8 序列化字节和协议余量保守估算。请将窗口配置为部署模型的实际限制。`memory_context_tokens` 和 `tool_result_tokens` 分别限制参考记忆和单条工具正文。

优先保留当前输入、最新工具调用及结果和最近对话轮次。旧工具轮次按整组裁剪。存不下最新工具轮次时，保留已核实结果并以 `partial` 结束。当前输入本身装不下时，同步返回 422，流式返回 `error`，原因均为 `context_budget`。正常终态的 `context_warnings` 提示历史、参考记忆或工具输出有省略，前端在对应回答下显示。

模型只收到本轮生效的画像条目和去重的召回片段。未选场景时仅使用全局偏好。已选场景时对应偏好覆盖同键全局值。知识片段保留记忆 ID、来源及可用的文件名，不随模型负载发送完整存储记录。界面召回证据仍由现有返回字段提供。自动摘要是否需要由长会话实测决定，当前使用确定性裁剪。
