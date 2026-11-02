<#
.SYNOPSIS  Start the allow-list proxy on 127.0.0.1:8800 (only POST /v1/chat/completions -> runtime 127.0.0.1:8000). LOCAL ONLY.
           This script does NOT start cloudflared and exposes nothing. The pid is recorded in integration/.run/pids.json
           so integration\stop_all.ps1 stops it.
.PARAMETER Python   Python with starlette, httpx and uvicorn installed (the same venv as the runtime).
#>
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$Run = Join-Path $PSScriptRoot ".run"
New-Item -ItemType Directory -Force -Path $Run | Out-Null
$pf = Join-Path $Run "pids.json"
$pids = @{}
if (Test-Path $pf) { (Get-Content $pf -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $pids[$_.Name] = $_.Value } }
if ($pids.ContainsKey("voice_proxy") -and (Get-Process -Id ([int]$pids["voice_proxy"]) -ErrorAction SilentlyContinue)) { throw "voice proxy already running (pid $($pids['voice_proxy']))" }
. (Join-Path $PSScriptRoot "keygen.ps1")
# Entry key: the page is only reachable (cookie session granted) through  <tunnel>/voice/?k=<VOICE_PROXY_ENTRY_KEY>.  Kept if already in the
# shell or integration/.env, otherwise generated (cryptographic RNG) and appended to integration/.env. The value is never printed.
$EnvFile = Join-Path $PSScriptRoot ".env"
if (-not $env:VOICE_PROXY_ENTRY_KEY -and (Test-Path $EnvFile)) {
    $line = Get-Content $EnvFile | Where-Object { $_ -match '^\s*VOICE_PROXY_ENTRY_KEY\s*=\s*\S' } | Select-Object -First 1
    if ($line) { $env:VOICE_PROXY_ENTRY_KEY = (($line -split '=', 2)[1]).Trim().Trim('"') }
}
if (-not $env:VOICE_PROXY_ENTRY_KEY) {
    $env:VOICE_PROXY_ENTRY_KEY = New-RandomKey 32
    [IO.File]::AppendAllText($EnvFile, "`nVOICE_PROXY_ENTRY_KEY=$($env:VOICE_PROXY_ENTRY_KEY)`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Host "VOICE_PROXY_ENTRY_KEY generated and saved to integration/.env (value not printed)."
}
$env:PYTHONUTF8 = "1"; $env:VOICE_PROXY_PORT = "8800"; $env:VOICE_PROXY_UPSTREAM = "http://127.0.0.1:8000"; $env:VOICE_PROXY_VOICE_WEB = "1"
$p = Start-Process -FilePath $Python -ArgumentList @("-X", "utf8", (Join-Path $PSScriptRoot "voice_proxy.py")) -PassThru -WindowStyle Hidden `
     -RedirectStandardOutput (Join-Path $Run "voice_proxy.out.log") -RedirectStandardError (Join-Path $Run "voice_proxy.err.log")
$pids["voice_proxy"] = $p.Id
$pids | ConvertTo-Json | Set-Content $pf
$until = (Get-Date).AddSeconds(15)
do { Start-Sleep -Milliseconds 500; try { $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8800/anything" -TimeoutSec 2 } catch { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode; break } } } while ((Get-Date) -lt $until)
if ($code -ne 404) { throw "voice proxy did not answer 404 for an unknown path (got '$code'); see integration/.run/voice_proxy.err.log" }
Write-Host "voice proxy listening on 127.0.0.1:8800 (loopback only). Not exposed. Unknown paths answer 404; the page needs the entry key (integration/.env)."
