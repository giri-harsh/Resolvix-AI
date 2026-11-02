# Outbound calling: configuration, status and what is still needed

## Configuration source (canonical)
`integration/.env` (git-ignored) is the **only** file that reaches the runtime: `integration/run_all.ps1` loads it into the environment of every service it starts, and `voice-runtime` reads only `os.getenv` (no dotenv loader).
A `voice-runtime/.env` file is **not read by any code**; values placed only there have no effect. Keep the same values in `integration/.env` if they are needed.
This mock-only build does not read `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN` or `EXOTEL_ACCOUNT_SID` at all.

Variables (placeholders are in `integration/.env.example`): `OUTBOUND_ENABLED` (default false), `OUTBOUND_PROVIDER` (only `mock` is active; any other value gives a stub that refuses every call),
`OUTBOUND_PUBLIC_BASE_URL`, `EXOTEL_CALLER_ID`, `OUTBOUND_DB_PATH`, `OUTBOUND_MAX_CALL_SECONDS`, `OUTBOUND_RING_TIMEOUT_S`, `OUTBOUND_MAX_CONCURRENT`, `OUTBOUND_PER_USER_HOUR`, `OUTBOUND_PER_USER_DAY`,
`OUTBOUND_PER_DESTINATION_DAY`, `OUTBOUND_DAILY_SECONDS_CAP`, `OUTBOUND_ALLOWED_COUNTRY_CODES`, `OUTBOUND_ENFORCE_HOURS`, `OUTBOUND_HOURS_WINDOW`, `OUTBOUND_TOKEN_TTL_S`, `OUTBOUND_DUPLICATE_WINDOW_S`, `OUTBOUND_CONSENT_RETENTION_DAYS`, `OUTBOUND_BLOCKED_NUMBERS`.

## Run the offline tests
```powershell
cd voice-runtime
C:\rx\vint\Scripts\python.exe -m pytest tests/outbound -q        # no network, no provider, no real calls (a socket guard fails the test on any non-loopback connection)
```

## Status of this build
Implemented and tested with mocks only: API, state machine, SQLite persistence and audit log, validation, consent, emergency-number blocking, limits, idempotency, callbacks, cancellation, watchdog, the WebSocket bridge, mock ASR/TTS, Brain integration with isolated per-call sessions.
Not implemented on purpose: the real Exotel HTTP client, real ASR, real TTS, the public WebSocket relay in the proxy, any Ops/dashboard change.

## Blockers for a real Exotel test
1. Exotel must confirm in writing: AgentStream/Voicebot (`StreamUrl` on `Calls/connect`) is enabled for our (trial) account and which API host serves it; how the bot ends a call (closing the socket? an event? an API?) and whether any hang-up API exists; which `media_format` is real; whether query parameters survive on `StreamUrl`; callback signing, retries and source IPs; `ringing` in `StatusCallbackEvents`; concurrent stream limit; ISD enablement, countries and rates.
2. Trial limits: calls only to up to 10 whitelisted numbers, the assigned trial ExoPhone as caller ID (`EXOTEL_CALLER_ID` is not set yet), trial credits expire (7 or 15 days: the docs disagree).
3. Credentials we do not have: an ASR key and a TTS key (Hindi-capable) and the real adapters for them; a stable public `wss://` host (a quick tunnel changes URL on every restart); a WebSocket relay in the allow-list proxy.
4. Compliance owner decisions: consent capture process, calling-hour policy, AI-voice disclosure wording (the greeting already says it is an AI and may be recorded), destination-country rules for any international number.
5. Pricing: Exotel publishes no per-minute prices in its developer docs; get the rate card from the Exotel dashboard (Settings > Billing > Rate Card) or the account manager. Nothing here estimates cost.

## One safe test call (only after the blockers above are cleared and you explicitly approve it)
Use your own whitelisted number, `OUTBOUND_MAX_CALL_SECONDS=60`, `OUTBOUND_ENABLED=true`, a real provider build, and watch the audit log. Do not run it before then.
