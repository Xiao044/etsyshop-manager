[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$StateFile = Join-Path $RepoRoot 'data\local-processes.json'
if (-not (Test-Path -LiteralPath $StateFile)) { throw 'No managed local process state found.' }
$State = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
$LocalScript = Join-Path $RepoRoot 'scripts\local.py'
$ProcessIds = @([int]$State.webPid)
# Backward compatibility: stop an older managed manual-refresh Worker once.
if ($State.PSObject.Properties.Name -contains 'workerPid' -and $State.workerPid) {
    $ProcessIds += [int]$State.workerPid
}
foreach ($ProcessId in $ProcessIds) {
    $Parent = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $Parent) { continue }
    if ($Parent.Name -ne 'python.exe' -or $Parent.CommandLine -notlike "*$LocalScript*") {
        throw "PID $ProcessId no longer belongs to this project's local launcher; refusing to stop it."
    }
    # Stop only the verified launcher tree, never a computed arbitrary PID or browser name.
    $Pending = [System.Collections.Generic.List[int]]::new()
    $Pending.Add([int]$Parent.ProcessId)
    $Processes = @(Get-CimInstance Win32_Process)
    for ($Index=0; $Index -lt $Pending.Count; $Index++) {
        foreach ($Child in @($Processes | Where-Object { $_.ParentProcessId -eq $Pending[$Index] })) {
            $Pending.Add([int]$Child.ProcessId)
        }
    }
    for ($Index=$Pending.Count-1; $Index -ge 0; $Index--) {
        Stop-Process -Id $Pending[$Index] -ErrorAction SilentlyContinue
    }
}
Write-Output 'Stopped the managed local website and any legacy manual-refresh Worker. The server daily collector is not changed.'
