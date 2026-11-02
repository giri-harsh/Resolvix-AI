# Findings for Member A (voice runtime). Nothing in `voice-runtime/**` was edited.

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
# Voice bring-up blockers for Member A (checked against 176891c; `agora_control.py` is unchanged by that commit)
Please fix these in your own files today (Harsh/Kamal asked). Only if they cannot be done within a few hours do we ask you to stop editing `voice-runtime/**` so Kamal can take over: never both at once.

## A-6 (blocker): the RTC token is not an Agora token
- **Where:** `voice-runtime/resolvix_runtime/agora_control.py:21-46`. `return f"006{app_id}{encoded_mac[:32]}"` is a hand-rolled HMAC string (lines 43-46), not Agora's token format.
- **Expected:** Agora AccessToken2 ("007"), built with Agora's open-source token builders (`RtcTokenBuilder2.buildTokenWithUid(appId, appCertificate, channelName, uid, role, tokenExpire, privilegeExpire)`). Reference: https://docs.agora.io/en/realtime-media/im/build/secure-access-and-authentication/access-token-2.md (the same builders back the RTC docs; Python: Agora's `AgoraDynamicKey` repository).
- **Why it matters:** `static/call.html:126-129` passes this token to `rtcClient.join(...)`; with a real App Certificate enabled on the Agora project the join will be rejected. The agent token (line ~129 `generate_rtc_token(..., 0)`) must also be a real token for the agent's uid.

**Verified fix path for A-6 (2026-11-02):** Agora's official repo https://github.com/AgoraIO/Tools, folder `DynamicKey/AgoraDynamicKey/python3/src`. `RtcTokenBuilder2.RtcTokenBuilder.build_token_with_uid(app_id, app_certificate, channel, uid, Role_Publisher, token_expire, privilege_expire)` returns a token starting with `007`; with the real App ID and certificate, a browser (`AgoraRTC_N.js`) joined a channel with it: `connection: CONNECTING -> CONNECTED`, "RESULT: PASS - connected with a 007 token" (reproduce with `integration/agora_join_test/`). The same repo has `convoai_token_builder.py`: `ConvoAITokenBuilder.build_token(app_id, app_certificate, channel_name, account, token_expire, privilege_expire)` builds the **RTC + RTM** token meant for the Conversational AI agent: use it for the agent's `properties.token` (A-7). Vendor the three files (`AccessToken2.py`, `Packer.py`, `RtcTokenBuilder2.py`, `convoai_token_builder.py`) or add a small dependency; the repo is MIT-style Agora sample code (check the license before copying).

## A-7 (blocker): the agent join payload has the wrong shape
- **Where:** `agora_control.py:118-155`. Posts `{"name", "channel", "rtc": {"token"}, "parameters": {"greeting", "llm", "asr", "tts"}}`.
- **Expected** (Agora Conversational AI Engine, `POST https://api.agora.io/api/conversational-ai-agent/v2/projects/{appid}/join`): `{"name": <unique>, "properties": {"channel": ..., "token": <agent token>, "agent_rtc_uid": "<uid string>", "remote_rtc_uids": ["<browser uid>"], "llm": {"url": ..., "api_key": ..., ...}, "asr": {...}, "tts": {...}}}`. Reference: https://docs.agora.io/en/conversational-ai/rest-api/agent/join (docs differ between versions, so check the page that matches your project). The greeting belongs to the agent settings, not `parameters`.
- Stop the agent when the call ends (`leave` API) so minutes are not burned.

## A-8 (blocker for Hindi/Hinglish): no ASR language and no real TTS voice
- `agora_control.py:143-146` sends `asr: {vendor, params: {api_key}}` with no `language`/`model`; Deepgram Nova-3 needs `model: nova-3` and `language: multi` (or `hi`) for Hindi-English speech (https://developers.deepgram.com/docs/nova-3-language-prompting.md).
- `agora_control.py:147-153` falls back to `voice_id: "default"` (not a valid ElevenLabs voice id) and does not set the model (`eleven_flash_v2_5` is the low-latency one that includes Hindi; verify). Put `ASR_LANGUAGE`, `ASR_MODEL`, `TTS_MODEL` in `config.py` and `.env.example`.
- Agora also offers **managed mode** (`credential_mode: "managed"` inside the `asr`/`llm`/`tts` block; Agora bills the vendor, no key needed). See NEEDED_KEYS.md: ASR (Deepgram nova-2/nova-3) and the MiniMax TTS are managed; ElevenLabs is key-based only. Please make the payload able to use either mode (`ASR_CREDENTIAL_MODE`, `TTS_CREDENTIAL_MODE`).

## A-9 (required by Harsh): a failed agent join must be an error, not a warning
- `agora_control.py:158-169`: non-200 -> `logger.warning(...)`, exception -> `logger.warning(...)`, and `start_web_session` (line 102-109) still returns the session, so the browser joins an empty channel in silence.
- **Expected:** non-2xx or exception -> `logger.error` with the Agora error body (mask keys), raise (HTTP 502 from `/api/call/start` or the current start route with `{"error": {"code": "agent_join_failed", "message": ...}}`), end the ops call record, and show the message on `call.html` (the `catch (err)` at `static/call.html:142` only does `console.error`). The 4 s client timeout (line 158) is also short for a cold agent start; use 10 s.

## A-10: nothing in the shim was tested against Agora yet
`/v1/chat/completions` is covered by unit tests only. Once keys exist we will check: the Authorization bearer, `call_id` query parameter, `stream: true`, and the SSE chunk format Agora expects.

