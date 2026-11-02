<#
.SYNOPSIS  Stop only the processes started by run_all.ps1 (recorded in integration/.run/pids.json), including their child processes.
#>
$ErrorActionPreference = "Continue"
$f = Join-Path $PSScriptRoot ".run\pids.json"
if (-not (Test-Path $f)) { Write-Host "Nothing recorded (integration/.run/pids.json not found)."; exit 0 }
$pids = Get-Content $f -Raw | ConvertFrom-Json
foreach ($p in $pids.PSObject.Properties) {
    $id = [int]$p.Value
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
        & taskkill.exe /PID $id /T /F | Out-Null
        Write-Host "stopped $($p.Name) (pid $id)"
    } else { Write-Host "$($p.Name) (pid $id) was not running" }
}
Remove-Item -Force $f
