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

function Update-ClerkTaskBranch {
    # Bound the network operation. The local fast-forward runs separately so
    # the timeout cannot interrupt an update to the working tree.
    $fetchInfo = New-Object System.Diagnostics.ProcessStartInfo
    $fetchInfo.FileName = (Get-Command git -CommandType Application | Select-Object -First 1).Source
    $fetchInfo.Arguments = 'fetch --no-recurse-submodules --no-auto-maintenance origin'
    $fetchInfo.WorkingDirectory = $taskRoot
    $fetchInfo.UseShellExecute = $false
    $fetchInfo.CreateNoWindow = $true
    $fetchInfo.RedirectStandardOutput = $true
    $fetchInfo.RedirectStandardError = $true
    $fetchInfo.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
    $fetchInfo.EnvironmentVariables['GCM_INTERACTIVE'] = 'Never'

    try {
        $fetchProcess = [System.Diagnostics.Process]::Start($fetchInfo)
    } catch {
        Write-Warning 'Git fetch could not start. Clerk is showing local tasks.'
        return
    }
    try {
        $fetchProcess.BeginOutputReadLine()
        $fetchProcess.BeginErrorReadLine()
        if (-not $fetchProcess.WaitForExit(5000)) {
            & taskkill.exe /PID $fetchProcess.Id /T /F 2>$null | Out-Null
            Write-Warning 'Git fetch timed out after 5 seconds. Clerk is showing local tasks.'
            return
        }
        if ($fetchProcess.ExitCode -ne 0) {
            Write-Warning 'Git fetch failed. Clerk is showing local tasks.'
            return
        }
    } finally {
        $fetchProcess.Dispose()
    }

    $taskBranchAfterFetch = git -C $taskRoot branch --show-current
    $taskChangesAfterFetch = git -C $taskRoot status --porcelain --untracked-files=all
    if ($LASTEXITCODE -ne 0 -or $taskBranchAfterFetch -ne 'master' -or $taskChangesAfterFetch) {
        Write-Warning 'The worktree changed during fetch. Clerk is showing local tasks.'
        return
    }
    git -C $taskRoot -c submodule.recurse=false merge --ff-only --no-autostash --quiet refs/remotes/origin/master 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'The branch could not fast-forward to origin/master. Clerk is showing local tasks.'
    }
}

# Clerk reads local records. Refresh the shared master before task queries
# through this entry point, while personal branches keep their own records.
if ($args.Count -ge 2 -and $args[0] -eq 'tasks' -and $args[1] -in @('list', 'show') -and
    '--help' -notin $args -and '-h' -notin $args) {
    $taskBranch = git -C $taskRoot branch --show-current
    $taskOrigin = git -C $taskRoot config --get remote.origin.url
    if ($taskBranch -eq 'master' -and
        $taskOrigin -match '^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)IweirdoI/(?:OmniAble|mokoa-windows)(?:\.git)?/?$') {
        $taskChanges = git -C $taskRoot status --porcelain --untracked-files=all
        if ($LASTEXITCODE -ne 0 -or $taskChanges) {
            Write-Warning 'Clerk is showing local tasks because the worktree could not be verified clean.'
        } else {
            Update-ClerkTaskBranch
        }
    }
}
& $taskExecutable --root $taskRoot @args
exit $LASTEXITCODE
