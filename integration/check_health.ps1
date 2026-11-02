<#
.SYNOPSIS  Print the health of ops (8100), the voice runtime / real Brain (8000) and the dashboard (5173). Exit code 1 if any is down.
#>
$bad = 0
function Probe([string]$name, [string]$url) {
    try { $r = Invoke-RestMethod -Uri $url -TimeoutSec 4; Write-Host ("OK   {0,-14} {1}" -f $name, ($r | ConvertTo-Json -Compress -Depth 4)); return $true }
    catch { Write-Host ("DOWN {0,-14} {1}  ({2})" -f $name, $url, $_.Exception.Message); return $false }
}
if (-not (Probe "ops-api" "http://localhost:8100/healthz")) { $bad = 1 }
if (-not (Probe "voice-runtime" "http://localhost:8000/healthz")) { $bad = 1 }
try { $r = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:5173/" -TimeoutSec 4; Write-Host "OK   dashboard      http://localhost:5173/ ($($r.StatusCode))" } catch { Write-Host "DOWN dashboard      http://localhost:5173/"; $bad = 1 }
exit $bad
