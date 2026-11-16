[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8765)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$LocalScript = Join-Path $RepoRoot 'scripts\local.py'
$Data = Join-Path $RepoRoot 'data'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Run scripts/setup-windows.ps1 first.' }
if (-not (Test-Path -LiteralPath (Join-Path $Data 'shops.sqlite3'))) { throw 'Local SQLite database is missing. Run setup first.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) { throw "Port $Port is already in use. Stop the old local server explicitly before restarting." }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$Logs = Join-Path $Data 'logs'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$Web = Start-Process -FilePath $Python -ArgumentList @('-u', "`"$LocalScript`"", 'web', '--port', "$Port") -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Logs 'web.stdout.log') -RedirectStandardError (Join-Path $Logs 'web.stderr.log') -PassThru
@{ webPid=$Web.Id; port=$Port; startedAt=(Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Data 'local-processes.json') -Encoding utf8
Write-Output "Local website: http://127.0.0.1:$Port"
Write-Output "Web PID: $($Web.Id). Logs: $Logs"
Write-Output 'Manual refresh is removed. This launcher does not start a collection Worker or a Windows scheduled task.'
Write-Output 'Closing this terminal will not stop background services. Use scripts/stop-windows.ps1 to stop them.'
