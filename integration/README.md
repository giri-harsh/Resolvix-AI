# Running Resolvix end to end on one machine

Files in this folder (new; `e2e_test.py` and `INTEGRATION_LOG.md` are Member A's and were not touched):

| File | What it does |
|---|---|
| `run_all.ps1` | Seeds and starts Ops API (8100), Voice Runtime with the **real Brain** (8000) and the dashboard (5173); waits for each health check; refuses to continue if the runtime fell back to MockBrain. |
| `stop_all.ps1` | Stops only the processes `run_all.ps1` started (`.run/pids.json`). |
| `check_health.ps1` | Health of the three services. |
| `e2e_scenarios.py` | Scenarios a-i through the runtime's typed test endpoint, verified in the ops API (35 checks). |
| `e2e_publish_and_drills.py` | J6 live KB publish and the duplicate-delivery / ops-down drills. |
| `.env.example` | Names only. Real values live in `integration/.env` (git-ignored). |
| `INTEGRATION_REPORT.md`, `FINDINGS_FOR_A.md`, `FINDINGS_FOR_C.md` | Results and evidence. |

## One-time setup (PowerShell, repo root)
```powershell
py -3.11 -m venv .venv          # 3.12 also worked
.\.venv\Scripts\python.exe -m pip install -e contracts/python -e "brain[dev]" -e "voice-runtime[dev]" -e "ops/api[dev]"
.\.venv\Scripts\python.exe -m pip install -r ops/api/requirements.txt -r ops/api/requirements-dev.txt   # Member C's pinned versions
npm --prefix ops/dashboard ci
copy ops\api\.env.example ops\api\.env     # only needed for ops/api/dev/smoke.py and e2e_journeys.py
```
The first Brain start downloads the multilingual-e5-small model (~470 MB) into `brain/.cache`.

## Run
```powershell
integration\run_all.ps1 -Python .\.venv\Scripts\python.exe              # LLM mock (no Groq key needed)
integration\run_all.ps1 -Python .\.venv\Scripts\python.exe -LlmMode live  # needs GROQ_API_KEY in integration\.env
integration\check_health.ps1 ; integration\stop_all.ps1
.\.venv\Scripts\python.exe -X utf8 integration\e2e_scenarios.py
```
If `integration/.env` does not exist, `run_all.ps1` creates it with random **local-only** secrets (never printed). Put
`RUNTIME_MODE=live` in it to make the dashboard's Publish button call the runtime (J6). Typed test console: http://localhost:8000/test.

## How the shared configuration is read (one key everywhere)
| Variable | Ops (`ops/api`) | Voice runtime | Brain (runs inside the runtime process) |
|---|---|---|---|
| `SERVICE_KEY` | required; checks `X-Service-Key` | sent on every ops call and required on `/admin/kb/reload` | not read |
| `KB_EXPORT_SERVICE_KEY` | - | - | **must equal `SERVICE_KEY`**; `run_all.ps1` copies it |
| `KB_SOURCE` | - | - | `url:http://localhost:8100/v1/kb/export` |
| `OPS_API_URL` / `OPS_PUBLIC_URL` / `RUNTIME_URL` / `RUNTIME_MODE` | `OPS_PUBLIC_URL`, `RUNTIME_URL`, `RUNTIME_MODE` | `OPS_API_URL` | - |
| `BRAIN_IMPL` | - | `real` (a failed import raises; verified) | - |
| `LLM_MODE`, `GROQ_API_KEY`, `LLM_*`, `EMBED_*` | - | - | read from the process environment or `brain/.env` |
| `SESSION_SECRET`, `SEED_PASSWORD` | ops only | - | - |

The runtime reads only the process environment (no `.env` loading), so `run_all.ps1` exports everything.

## Not covered here
Real voice (microphone and speaker), Agora, ASR/TTS vendors and phone telephony need credentials and a public HTTPS URL that this
machine does not have: see `INTEGRATION_REPORT.md`, "Walls".
