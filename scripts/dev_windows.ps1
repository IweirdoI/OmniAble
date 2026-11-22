# 在 Windows 上启动 API。默认 memory + hash，无需下载 BGE。
# .\scripts\dev_windows.ps1
# .\scripts\dev_windows.ps1 -Port 18080 -Embedding local

param(
    [int]$Port = 8000,
    [ValidateSet("hash", "local")]
    [string]$Embedding = "hash",
    [ValidateSet("memory", "milvus")]
    [string]$MemoryBackend = "memory"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "windows_env.ps1")
$env:OS_AGENT_MEMORY_BACKEND = $MemoryBackend
$env:OS_AGENT_EMBEDDING_BACKEND = $Embedding

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "未找到 .venv。请先运行 .\scripts\setup_windows.ps1"
}

if ($Embedding -eq "local") {
    & $python -c "import sentence_transformers"
    if ($LASTEXITCODE -ne 0) {
        throw "当前环境没有 sentence-transformers。请先运行 .\scripts\setup_windows.ps1 -Profile full"
    }
}

Write-Host "API http://127.0.0.1:$Port  memory=$MemoryBackend  embedding=$Embedding"
& $python -m uvicorn api.server:app --host 127.0.0.1 --port $Port
