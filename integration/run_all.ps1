<#
.SYNOPSIS  Start Ops API (8100), Voice Runtime with the REAL Brain (8000) and the dashboard (5173) on this machine.
.PARAMETER Python   Python 3.11+ that has contracts, brain, voice-runtime and ops/api installed (see integration/README.md).
.PARAMETER LlmMode  mock (no Groq key needed, default) or live (needs GROQ_API_KEY in integration/.env or the environment).
.PARAMETER NoDashboard  Skip npm run dev.
Secrets: values come from integration/.env (git-ignored). If it does not exist it is created with random LOCAL-ONLY values.
Nothing secret is printed. Only processes started here are recorded in integration/.run/pids.json (used by stop_all.ps1).
#>
param(
    [string]$Python = "python",
    [ValidateSet("mock", "live")][string]$LlmMode = "mock",
    [switch]$NoDashboard,
    [switch]$OnlyOps,          # (re)start just the Ops API, e.g. after a failure drill; keeps the other recorded pids
    [switch]$ResetOpsDb
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Run = Join-Path $PSScriptRoot ".run"
$EnvFile = Join-Path $PSScriptRoot ".env"
New-Item -ItemType Directory -Force -Path $Run | Out-Null

. (Join-Path $PSScriptRoot "keygen.ps1")     # New-RandomKey / New-RandomString / Initialize-ShimApiKey (cryptographic RNG, never printed)

# ---- shared environment ------------------------------------------------------------------------------------------
if (-not (Test-Path $EnvFile)) {
    [IO.File]::WriteAllLines($EnvFile, @(
        "# LOCAL ONLY, git-ignored. One SERVICE_KEY is used by ops, the runtime and (as KB_EXPORT_SERVICE_KEY) the brain.",
        "SERVICE_KEY=$(New-RandomKey 32)", "SESSION_SECRET=$(New-RandomKey 48)", "SEED_PASSWORD=$(New-RandomString 16)",
        "GROQ_API_KEY="
    ), (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Created integration/.env with random local-only secrets (not printed). SHIM_API_KEY is added below."
}
Get-Content $EnvFile | ForEach-Object {
    $l = $_.Trim()
    if ($l -and -not $l.StartsWith("#") -and $l.Contains("=")) {
        $i = $l.IndexOf("="); $name = $l.Substring(0, $i).Trim(); $val = $l.Substring($i + 1).Trim().Trim('"')
        if ($val) { Set-Item -Path "env:$name" -Value $val }
    }
}
foreach ($need in "SERVICE_KEY", "SESSION_SECRET", "SEED_PASSWORD") {
    if (-not (Get-Item "env:$need" -ErrorAction SilentlyContinue)) { throw "$need is missing in integration/.env" }
}
$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
# ops
$env:PORT = "8100"; $env:OPS_PUBLIC_URL = "http://localhost:8100"; $env:CORS_ORIGINS = "http://localhost:5173"
$env:NOTIFY_MODE = "mock"; $env:TIMEZONE = "Asia/Kolkata"
if (-not $env:RUNTIME_MODE) { $env:RUNTIME_MODE = "mock" }       # set live in integration/.env to make Publish call the runtime
$env:RUNTIME_URL = "http://localhost:8000"
$env:DATABASE_URL = "sqlite:///./data/resolvix.db"
# runtime + brain (the Brain runs inside the runtime process)
$env:OPS_API_URL = "http://localhost:8100"; $env:TELEPHONY_MODE = "mock"
# PUBLIC_BASE_URL = the URL Agora's cloud uses to reach POST /v1/chat/completions. Default: local. To use a tunnel, set it in the shell
# or in integration/.env (never hard-code a tunnel URL here). A non-local value must be a bare https origin.
if (-not $env:PUBLIC_BASE_URL) { $env:PUBLIC_BASE_URL = "http://localhost:8000" }
elseif ($env:PUBLIC_BASE_URL -notmatch '^https://[A-Za-z0-9.-]+(:\d+)?/?$' -and $env:PUBLIC_BASE_URL -notmatch '^http://(localhost|127\.0\.0\.1)(:\d+)?/?$') {
    throw "PUBLIC_BASE_URL must be a bare https origin (the tunnel URL) or a localhost URL. The value is not printed."
}
# SHIM_API_KEY: keep an explicitly supplied one (shell or integration/.env); otherwise generate 32 random bytes and persist them to integration/.env.
# The runtime reads it from this environment and uses the same value in the Agora agent's llm.api_key, so there is a single source of truth.
[void](Initialize-ShimApiKey -EnvFile $EnvFile -PublicBaseUrl $env:PUBLIC_BASE_URL)
# Agora-managed speech (no ASR/TTS keys needed): Deepgram nova-3 + MiniMax. Override any of these in the shell or integration/.env.
if (-not $env:ASR_CREDENTIAL_MODE) { $env:ASR_CREDENTIAL_MODE = "managed" }
if (-not $env:ASR_VENDOR) { $env:ASR_VENDOR = "deepgram" }
if (-not $env:ASR_MODEL) { $env:ASR_MODEL = "nova-3" }
if (-not $env:ASR_LANGUAGE) { $env:ASR_LANGUAGE = "hi" }
if (-not $env:TTS_CREDENTIAL_MODE) { $env:TTS_CREDENTIAL_MODE = "managed" }
if (-not $env:TTS_VENDOR) { $env:TTS_VENDOR = "minimax" }
if (-not $env:TTS_MODEL) { $env:TTS_MODEL = "speech-2.8-turbo" }
if (-not $env:TTS_VOICE_ID) { $env:TTS_VOICE_ID = "English_captivating_female1" }   # verified audible; "Hindi_female_1_v1" is NOT a valid managed voice (agent stays silent)
if (-not $env:TTS_LANGUAGE_BOOST) { $env:TTS_LANGUAGE_BOOST = "Hindi" }
$env:BRAIN_IMPL = "real"
$env:LLM_MODE = $LlmMode
$env:KB_SOURCE = "url:http://localhost:8100/v1/kb/export"
$env:KB_EXPORT_SERVICE_KEY = $env:SERVICE_KEY
$env:EMBED_CACHE_DIR = Join-Path $Root "brain\.cache\fastembed"   # one model download (~470 MB) shared by every start
if ($LlmMode -eq "live" -and -not $env:GROQ_API_KEY) { throw "LLM live needs GROQ_API_KEY in integration/.env or the environment" }

# ---- helpers -----------------------------------------------------------------------------------------------------
$pids = @{}
$pf = Join-Path $Run "pids.json"
if (Test-Path $pf) { (Get-Content $pf -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $pids[$_.Name] = $_.Value } }
function Wait-Http([string]$url, [int]$seconds, [string]$what) {
    $until = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $until) {
        try { $r = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3; if ($r.StatusCode -lt 500) { Write-Host "  OK  $what  $url"; return } } catch { Start-Sleep -Milliseconds 700 }
    }
    throw "$what did not become healthy at $url within $seconds s (see integration/.run/*.log)"
}
function Start-Svc([string]$name, [string]$exe, [string[]]$argList, [string]$cwd) {
    $out = Join-Path $Run "$name.out.log"; $err = Join-Path $Run "$name.err.log"
    $p = Start-Process -FilePath $exe -ArgumentList $argList -WorkingDirectory $cwd -PassThru -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err
    $pids[$name] = $p.Id
    $pids | ConvertTo-Json | Set-Content (Join-Path $Run "pids.json")
    return $p
}

# ---- 1. Ops API ----------------------------------------------------------------------------------------------------
$OpsDir = Join-Path $Root "ops\api"
if ($ResetOpsDb) { Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $OpsDir "data\resolvix.db*") }
Write-Host "Seeding ops (idempotent)..."
Push-Location $OpsDir; & $Python -X utf8 dev\seed.py | Out-Null; Pop-Location
Write-Host "Starting Ops API on :8100"
Start-Svc "ops" $Python @("-X", "utf8", "-m", "uvicorn", "resolvix_ops.app:app", "--port", "8100", "--loop", "asyncio", "--workers", "1") $OpsDir | Out-Null
Wait-Http "http://localhost:8100/healthz" 40 "ops-api"

if ($OnlyOps) { Write-Host "Ops API restarted (only)."; return }

# ---- 2. Voice runtime + real Brain ------------------------------------------------------------------------------------
Write-Host "Starting Voice Runtime on :8000 (BRAIN_IMPL=real, LLM_MODE=$LlmMode, KB from ops)"
Start-Svc "runtime" $Python @("-X", "utf8", "-m", "uvicorn", "resolvix_runtime.app:app", "--port", "8000") (Join-Path $Root "voice-runtime") | Out-Null
Wait-Http "http://localhost:8000/healthz" 120 "voice-runtime"
$h = Invoke-RestMethod -Uri "http://localhost:8000/healthz"
if (-not $h.brain.embedder) { throw "Runtime is NOT using the real Brain (health.brain has no 'embedder'): $($h.brain | ConvertTo-Json -Compress). Check integration/.run/runtime.err.log" }
Write-Host ("  real Brain: embedder={0} kb_version={1} llm={2} llm_mode={3}" -f $h.brain.embedder, $h.brain.kb_version, $h.brain.llm, $h.brain.llm_mode)

# ---- 3. Dashboard -------------------------------------------------------------------------------------------------------
if (-not $NoDashboard) {
    $dash = Join-Path $Root "ops\dashboard"
    if (-not (Test-Path (Join-Path $dash "node_modules"))) { Write-Host "npm ci (first run)..."; Push-Location $dash; npm ci --no-audit --no-fund | Out-Null; Pop-Location }
    $npm = (Get-Command npm.cmd).Source
    Write-Host "Starting dashboard on :5173"
    Start-Svc "dashboard" $npm @("run", "dev", "--", "--port", "5173", "--strictPort") $dash | Out-Null
    Wait-Http "http://localhost:5173/" 60 "dashboard"
}

Write-Host ""
Write-Host "Ops API        http://localhost:8100/healthz"
Write-Host "Voice runtime  http://localhost:8000/healthz   typed test console: http://localhost:8000/test"
if (-not $NoDashboard) { Write-Host "Dashboard      http://localhost:5173   (admin@demo / exam@demo / accounts@demo / hostel@demo, password = SEED_PASSWORD in integration/.env)" }
Write-Host "Stop with:     integration\stop_all.ps1      Health: integration\check_health.ps1"
