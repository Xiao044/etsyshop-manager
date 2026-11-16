[CmdletBinding()]
param([string]$Snapshot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path -LiteralPath $Python)) {
    & py -3.12 -m venv (Join-Path $RepoRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 venv creation failed.' }
}
& $Python -m pip install --disable-pip-version-check -r (Join-Path $RepoRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$LocalScript = Join-Path $RepoRoot 'scripts\local.py'
if ($Snapshot) {
    & $Python $LocalScript init-db --snapshot $Snapshot
} else {
    & $Python $LocalScript init-db
}
if ($LASTEXITCODE -ne 0) { throw 'Local SQLite initialization failed.' }
Write-Output 'Ready. Run .\.venv\Scripts\python.exe scripts\local.py web'
