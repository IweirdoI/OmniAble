# Install the pinned upstream release into this clone, without changing user PATH.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskVersion = '0.1.0'
$taskInstall = Join-Path $taskRoot ".tools/clerk/$taskVersion"
$taskTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("mokoa-clerk-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $taskTemp | Out-Null
$taskAsset = "clerk-$taskVersion-windows-x86_64.zip"
$taskBase = "https://github.com/invefa/Clerk/releases/download/v$taskVersion"
Invoke-WebRequest "$taskBase/$taskAsset" -OutFile (Join-Path $taskTemp $taskAsset) -TimeoutSec 60
Invoke-WebRequest "$taskBase/SHA256SUMS" -OutFile (Join-Path $taskTemp 'SHA256SUMS') -TimeoutSec 30
$taskLine = Get-Content (Join-Path $taskTemp 'SHA256SUMS') | Where-Object { $_ -match ([regex]::Escape($taskAsset) + '$') }
if (@($taskLine).Count -ne 1) { throw 'Expected exactly one checksum entry for the release archive.' }
$taskExpected = ($taskLine -split '\s+')[0]
$taskPinned = '0fdd3ff48d21c08d51fed5df00058fee5172c24e189b1e0db565571e74df94e4'
if ($taskExpected -ine $taskPinned) { throw 'The upstream checksum differs from the reviewed release. Review the new package before updating the installer.' }
$taskActual = (Get-FileHash (Join-Path $taskTemp $taskAsset) -Algorithm SHA256).Hash
if ($taskActual -ine $taskExpected) { throw 'Clerk archive checksum mismatch.' }
if (Test-Path $taskInstall) { throw "Install directory already exists: $taskInstall. Keep it or move it before reinstalling." }
New-Item -ItemType Directory -Path $taskInstall | Out-Null
Expand-Archive (Join-Path $taskTemp $taskAsset) -DestinationPath $taskInstall
$taskBins = @(Get-ChildItem -LiteralPath $taskInstall -Filter clerk.exe -Recurse)
if ($taskBins.Count -ne 1) { throw 'Expected exactly one clerk.exe in the release archive.' }
$taskIdentity = & $taskBins[0].FullName version --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $taskIdentity.version -ne $taskVersion -or $taskIdentity.dirty) { throw 'Unexpected Clerk build identity.' }
Write-Host "Installed Clerk $taskVersion; commit $($taskIdentity.commit)"
Write-Host 'Run: ./scripts/clerk.ps1 check'
Write-Host "Downloaded archive and checksum retained at: $taskTemp"
