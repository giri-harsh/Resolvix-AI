# Voice bring-up plan (web-call page, browser microphone) and the public tunnel

Owner of the plan: Kamal (demo machine). `voice-runtime/**` stays with Member A unless A and the team agree to hand it over (never both editing at once).
Open voice blockers for A are in `FINDINGS_FOR_A.md` (A-6 to A-10). Credentials: `NEEDED_KEYS.md`.

## Status (2026-11-02)
| Item | State |
|---|---|
| Ops, runtime with the real Brain, dashboard, live Groq LLM on this machine | WORKING (32/32 scenario checks on `llm_mode=live`, see PROGRESS.md) |
| Agora RTC token (A-6), agent join payload (A-7), ASR language + TTS voice (A-8), loud join failure (A-9) | NOT FIXED in `176891c` (A informed via FINDINGS_FOR_A.md) |
| Agora / Deepgram / ElevenLabs keys | waiting for Harsh |
| Public tunnel | NOT STARTED: `cloudflared` is not installed on the demo machine; install needs approval (see below) |

## The public tunnel (needed: Agora's cloud calls our custom-LLM URL)
Agora's agent calls `POST {PUBLIC_BASE_URL}/v1/chat/completions?call_id=...` with `Authorization: Bearer {SHIM_API_KEY}`. Only runtime port 8000 must be public. Ops (8100) and the dashboard (5173) stay local.

### Option 1: cloudflared quick tunnel (recommended: free, no account, HTTPS)
```powershell
winget install --id Cloudflare.cloudflared -e          # one-time download (needs approval to install software on this machine)
cloudflared tunnel --url http://localhost:8000           # prints https://<random-words>.trycloudflare.com
```
1. Keep that window open; copy the printed `https://....trycloudflare.com` URL.
2. Put `PUBLIC_BASE_URL=https://<that-host>` in `integration/.env` (the quick-tunnel URL changes every time cloudflared restarts, so update it and restart the runtime each time).
3. Restart only the runtime so it picks up the URL: `integration\stop_all.ps1` then `integration\run_all.ps1 -Python <python> -LlmMode live` (run_all currently pins `PUBLIC_BASE_URL=http://localhost:8000`; it must read the value from `.env` first: TODO when voice work starts).
4. Verify from outside: `curl https://<host>/healthz` shows the runtime health with `brain.embedder`.
5. Verify the shim's auth: `curl -i -X POST https://<host>/v1/chat/completions` must return 401 without the bearer token (the endpoint is public, the bearer is the only protection: keep `SHIM_API_KEY` long and random).

### Option 2: ngrok
`npm i -g ngrok` is already present on this machine as a shim but the binary does not run under Git Bash; use PowerShell: `ngrok config add-authtoken <token>` (needs an ngrok account) then `ngrok http 8000`. Free-tier URLs change on every start and show an interstitial page that can break server-to-server calls on some plans; prefer Option 1.

### Safety notes
- Only port 8000 is exposed; nothing else is forwarded. Stop the tunnel when not testing.
- The `/test` and `/call` pages are also reachable through the tunnel; they have no login. Do not leave the tunnel open unattended.

## Order of work once the keys arrive
1. A fixes A-6..A-9 (or hands over). Unit tests for the token builder and payload shape.
2. Start the tunnel, set `PUBLIC_BASE_URL`, start the stack.
3. Open `http://localhost:8000/call`, speak in Hindi, English and Hinglish: record STT transcript, Brain answer, TTS audio.
4. Check on the dashboard: call, turns, tickets, `alert.p1` for an emergency sentence; ticket before transfer; `{{ticket_id}}` spoken.
5. Latency per turn (end of speech to first audio: target < 1.5 s), barge-in (CR-8), then the spoken self-healing demo (needs C-1 fixed).
6. Phone (Agora PSTN/SIP or Twilio, DLT for India): documentation only until the web call works. Do not buy anything.

## Measured so far (typed console, live Groq, `gpt-oss-20b`, wall time of `POST /api/test/turn`, includes ops writes and mock SMS)
FAQ 450 ms (n=1), LLM answer 918 ms (n=1), escalation p50 1,363 ms (n=8), slot filling p50 167 ms, emergency 2,517 ms (n=1: ticket + 2 SMS + transfer are awaited in sequence). This is not speech latency: STT and TTS are not included. The emergency path's ops calls are a candidate for Harsh's "ops latency on the voice path" measurement.
