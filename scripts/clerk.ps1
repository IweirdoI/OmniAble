# Prefer this clone's pinned release, falling back to an existing Clerk installation.
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskInstall = Join-Path $taskRoot '.tools/clerk/0.1.0'
$taskBins = @(Get-ChildItem -LiteralPath $taskInstall -Filter clerk.exe -Recurse -ErrorAction SilentlyContinue)
if ($taskBins.Count -eq 1) {
    $taskExecutable = $taskBins[0].FullName
} else {
    $taskCommand = Get-Command clerk -CommandType Application -ErrorAction SilentlyContinue
    if (-not $taskCommand) { throw 'Install Clerk first: ./scripts/install_clerk.ps1' }
    $taskExecutable = $taskCommand.Source
}
& $taskExecutable --root $taskRoot @args
exit $LASTEXITCODE