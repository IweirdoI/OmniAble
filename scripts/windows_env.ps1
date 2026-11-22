# 当前 PowerShell 会话的 Windows 开发环境。变量只对当前会话及子进程生效。
# 用法: . .\scripts\windows_env.ps1

$Root = Split-Path -Parent $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $Root
if (-not $env:OS_AGENT_MEMORY_BACKEND) {
    $env:OS_AGENT_MEMORY_BACKEND = "memory"
}
if (-not $env:OS_AGENT_EMBEDDING_BACKEND) {
    $env:OS_AGENT_EMBEDDING_BACKEND = "hash"
}
