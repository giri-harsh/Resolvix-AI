# Findings for Member A (voice runtime). Nothing in `voice-runtime/**` was edited by Member B.

## A-1 (resolved in PR #9, verified): `BRAIN_IMPL=real` no longer falls back silently
`voice-runtime/resolvix_runtime/brain_loader.py:32-37` now raises `RuntimeError("BRAIN_IMPL is set to 'real', but resolvix_brain cannot be imported ...")`.
Verified by forcing the import to fail (`sys.modules['resolvix_brain'] = None`): the runtime raised instead of using MockBrain. `integration/run_all.ps1`
additionally checks that `/healthz` -> `brain.embedder` exists (only the real Brain reports it).

## A-2: the typed test endpoint never ends the call
`resolvix_runtime/app.py:116-` (`POST /api/test/turn`) starts the call and posts turns but never calls `ops_client.post_call_end` or `brain.end_call` (the production shim does: `shim.py:183-190`). Effects: every typed-test call stays "active" on the dashboard live board forever, "Answered by AI" stays 0, and Brain sessions are only freed by the 2 h TTL. Suggested: a `POST /api/test/end` (or end after N seconds idle).

## A-3: no caller number on the test channel
`app.py:124-129` creates `CallContext(caller_number=None)`, so on the typed console the Brain cannot send "ticket created" SMS to the caller (it asks for a number instead). Suggested: an optional `caller_number` field in `TestTurnRequest` so the SMS and callback demos can be shown without a phone.

## A-4 (observation): ops down makes a turn that needs a ticket wait ~10 s
With ops stopped, two typed turns (an answer, then a human request) took 20.6 s in total; the caller still got an answer and a "registered, staff will call" line, and the ticket was delivered after ops came back (queued ticket arrived as HS-1002). Consider a shorter ops timeout on the hot path so the caller is not silent for ~10 s.

## A-5 (observation): simulated transfers are recorded as `transfer_attempted` then `transfer_failed (reason: simulated)`
On the test channel every transfer shows as failed in the ticket timeline (`mode: simulated`). Harmless, but staff will see "transfer failed" on every typed test.

## Verified OK
Real Brain loads (`/healthz` brain.embedder = intfloat/multilingual-e5-small, KB fetched from ops with the key: `GET /v1/kb/export 200`, 12 entries); ticket-first (create_ticket before transfer, and before the staff_alert SMS); `{{ticket_id}}` spoken digit by digit; `/admin/kb/reload` rejects a wrong key (401) and returned KBVersionInfo with the right key; duplicate deliveries are idempotent; ops outage drill: answers continue, tickets queued and flushed on restart. Own suites: voice-runtime 16 passed; `integration/e2e_test.py` (Member A's) 9 passed against the running real-Brain stack.

---
# Voice bring-up blockers for Member A (A-6 to A-10)

## A-6 (blocker): the RTC token is not an Agora token
- **Where:** `voice-runtime/resolvix_runtime/agora_control.py:21-46`.
- **Resolution:** Implemented pure Python Agora AccessToken2 ("007") builder in `voice-runtime/resolvix_runtime/agora_token.py` based on Agora's official DynamicKey specification. Verified generating `"007..."` tokens with user and agent UIDs. Unit tested in `test_agora_integration.py::test_agora_token_builder_access_token_2`.

## A-7 (blocker): the agent join payload has the wrong shape
- **Where:** `agora_control.py:118-155`.
- **Resolution:** Payload updated to Agora Conversational AI REST API v2 schema: `properties` wrapper containing `channel`, `token` (agent AccessToken2), `agent_rtc_uid` ("1000"), `remote_rtc_uids` ([user_uid]), `llm` (with `greeting` and model parameters), `asr`, and `tts`. Implemented agent lifecycle teardown via `stop_agora_agent` invoking `POST /api/conversational-ai-agent/v2/projects/{appid}/agents/{agentId}/leave` upon call termination (`/api/sessions/web/end`). Unit tested in `test_agora_integration.py::test_agora_agent_join_payload_shape_and_voices` and `test_agora_agent_leave_on_session_end`.

## A-8 (blocker for Hindi/Hinglish): no ASR language and no real TTS voice
- **Where:** `agora_control.py:143-153`.
- **Resolution:** Configured Deepgram Nova-3 (`model: "nova-3"`, `language: "multi"` for seamless Hindi-English code-switching) and ElevenLabs low-latency model (`model_id: "eleven_flash_v2_5"`, default voice ID `pNInz6obpgDQGcFmaJgB`). Added support for Agora managed credentials mode (`credential_mode: "managed"` vs BYOK). Exposed `ASR_LANGUAGE`, `ASR_MODEL`, `ASR_CREDENTIAL_MODE`, `TTS_MODEL`, `TTS_VOICE_ID`, and `TTS_CREDENTIAL_MODE` in `config.py` and `.env.example`. Tested in `test_agora_integration.py::test_agora_agent_managed_mode`.

## A-9 (required by Harsh): a failed agent join must be an error, not a warning
- **Where:** `agora_control.py:158-169`.
- **Resolution:** Non-2xx status codes or network exceptions from Agora join endpoint now log an error, close the call in ops as `CallOutcome.failed`, and raise `HTTPException(status_code=502, detail={"error": {"code": "agent_join_failed", "message": ...}})`. Increased request timeout to 10s. Updated `static/call.html` to parse and render the error message on screen instead of failing silently. Tested in `test_agora_integration.py::test_agora_agent_join_failure_raises_502_and_closes_ops_call`.

## Emergency Path: Non-blocking SMS during Transfers
- **Where:** `executor.py:60-63`.
- **Resolution:** Updated `ActionExecutor.execute` to dispatch `send_sms` concurrently in background (`asyncio.create_task`) when `blocking=False` or during emergency tickets (`session.emergency = True`). Medical/emergency transfers are initiated immediately without waiting for SMS delivery roundtrips. Turn completion cleanly awaits pending tasks via `wait_pending()`. Tested in `test_agora_integration.py::test_emergency_path_does_not_wait_for_sms_to_transfer`.

## A-2 & A-3: Test Endpoint Enhancements
- **Resolution:** Added `caller_number` parameter to `TestTurnRequest` in `app.py` so typed tests can test SMS delivery and callback scheduling. Added `POST /api/test/end` to end the call, trigger ops call record closure with calculated outcome, and release Brain sessions.
