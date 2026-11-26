# Windows 开发

本文覆盖 OmniAble 在 Windows 上的安装、前后端联调、语义检索与性能验收。

产品方向及历史记忆系统的来源见 [README](../README.md) 与 [发布说明](发布说明.md)。

## 能证明什么

| 环境 | 能做 | 证据边界 |
|------|------|------------------|
| Windows + hash | 安装、API、规则抽取、单测 | 无法评估语义质量或产品延迟 |
| Windows + BGE-M3（`local`） | 语义检索和前后端联调 | 记录模型、维度以及实测机器 |
| 可选 Milvus 服务 | 重启后数据还在 | 单独验证存储持久化 |

Hash 向量用于接线。语义结果需使用本地模型且报告实际降级状态。

## 日常

前置：Python 3.12；`scripts/clerk.ps1` 依赖 PATH 里的 `git`（只装 GitHub Desktop 时其自带 git 不在 PATH，clerk 会报「identity is missing」的误导错误，先装独立 Git 或把 git 加入 PATH）；前端联调需要 Node.js。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1
```

第一条会创建 `.venv`，安装 `requirements-windows.txt`，必要时复制 `.env`，并用 memory + hash 跑 `scripts/smoke_e2e.py`。第二条在 http://127.0.0.1:8000 启动 API。注意：前端 Vite 代理默认指向 18080，要与前端联调请直接用 `dev_windows.ps1 -Port 18080`，不要按「默认端口起服务 → 前端代理报错」的顺序排查接口。

同一终端里要手动带上环境时：

```powershell
. .\scripts\windows_env.ps1
.\.venv\Scripts\python.exe -m pytest tests/test_preference_extract.py tests/test_preference_orchestrator_wire.py tests/test_api.py::test_health -q
```

`windows_env.ps1` 只改当前窗口：UTF-8、`PYTHONPATH`、记忆后端 `memory`、向量后端 `hash`。

提示：默认控制台是 GBK 编码，`curl -d "…中文…"` 会报 body 解析错误（TASK-002 复核）。测中文载荷请用 UTF-8 文件 `curl --data-binary @payload.json`，或改用 PowerShell `Invoke-RestMethod`。

## 语义检索

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Profile full
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1 -Embedding local
```

`full` 安装的是完整 `requirements.txt`，包含 `sentence-transformers`。模型体积大，需要能访问 HuggingFace 或镜像 `HF_ENDPOINT=https://hf-mirror.com`。验收脚本仍是 `scripts/windows_bge_acceptance.py`，隔离端口 18085。

前端另开终端：

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 http://localhost:5173 。默认代理见 `docs/LANGGRAPH_AGENT.md`，开发文档里的 API 端口是 18080：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1 -Port 18080
```

## TASK-007 目标配置验收

在目标 Windows 机器按实际产品配置选择存储。需要跨应用重启保留长期记忆时，预先启动 Milvus，并设置用于评测的服务地址。

```powershell
. ./scripts/windows_env.ps1
$env:OS_AGENT_EMBEDDING_BACKEND = "local"
# 若目标配置使用持久化存储，再设以下两项（按实际服务地址配置）
# $env:OS_AGENT_MEMORY_BACKEND = "milvus"
# $env:OS_AGENT_MILVUS_URI = "http://127.0.0.1:19530"
./.venv/Scripts/python.exe benchmarks/run_benchmarks.py --real
```

`--real` 测组件级检索。Milvus 模式在独立评测集合 `mokoa_windows_benchmark` 写入固定虚构数据，可用 `OS_AGENT_BENCHMARK_COLLECTION` 指定另一独立集合。切换模型或向量维度时另用一套评测集合。
启动带 Milvus 的 Windows API 时使用 `./scripts/dev_windows.ps1 -Port 18085 -Embedding local -MemoryBackend milvus`。
`scripts/windows_scale_acceptance.py` 可对端口 18085 做现有的串行规模探针，`benchmarks/run_scale_baseline.py` 提供固定数据的 memory/hash 并发对照。

完整验收使用同一提交和金标，记录 Windows 版本、CPU/GPU、内存、存储及模型版本、维度、降级状态、冷暖启动、规模和并发。
分别记录编码、存储、重排／画像、完整 recall 和聊天耗时，以及 Recall@K、P50/P95、吞吐、失败率。
以完整 recall 的 P95 对照 500ms 目标。按实测定位瓶颈，再用同一数据和配置复测，缺测单列。
