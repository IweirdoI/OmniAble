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

# Clerk reads local records. Refresh the shared master before task queries
# through this entry point, while personal branches keep their own records.
if ($args.Count -ge 2 -and $args[0] -eq 'tasks' -and $args[1] -in @('list', 'show') -and
    '--help' -notin $args -and '-h' -notin $args) {
    $taskBranch = git -C $taskRoot branch --show-current
    $taskOrigin = git -C $taskRoot config --get remote.origin.url
    if ($taskBranch -eq 'master' -and
        $taskOrigin -match '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)IweirdoI/mokoa-windows(?:\.git)?/?$') {
        $taskChanges = git -C $taskRoot status --porcelain --untracked-files=all
        if ($LASTEXITCODE -ne 0 -or $taskChanges) {
            Write-Warning 'Clerk is showing local tasks because the worktree could not be verified clean.'
        } else {
            $taskSyncResult = & $taskExecutable --root $taskRoot sync update 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Clerk could not sync with origin/master. Showing local tasks. $taskSyncResult"
            }
        }
    }
}
& $taskExecutable --root $taskRoot @args
exit $LASTEXITCODE
