# Voice-web API (v1) — browser voice chat ("Plan B")

The browser does speech-to-text and text-to-speech itself (Chrome). This API only moves **text** between the page and the Brain, and keeps
ops (calls, turns, tickets, SMS, "Answered by AI") consistent. Implemented in `voice-runtime/resolvix_runtime/voice_api.py`; the contract test is
`voice-runtime/tests/test_voice_web_api.py`. Owner: Member B.

Base URL: the runtime origin (local `http://localhost:8000`, or the tunnel URL Kamal posts to Harsh). All paths below are under `/api/voice`.
All bodies are JSON (UTF-8, Hindi/Hinglish/English text as normal strings).

## Auth and CORS
- CORS: allowed origins come from `VOICE_WEB_CORS_ORIGINS` (default `http://localhost:5174,http://127.0.0.1:5174`; add the tunnel origin, comma separated). No cookies are used (`credentials: 'omit'`).
- Optional bearer token: when `VOICE_WEB_API_KEY` is set, every request except the CORS preflight needs `Authorization: Bearer <key>`; otherwise `401` (the 401 still carries CORS headers). Not set in local development.
- The page never sees ops credentials, phone numbers of departments, or SMS bodies.

## Call lifecycle
```
POST /calls  ->  greeting (page speaks it)  ->  loop { page listens -> POST /calls/{id}/turns (SSE) -> page speaks sentences }  ->  POST /calls/{id}/end
```

### 1. Start a call — `POST /api/voice/calls`
Request (body optional):
```json
{ "caller_number": "+919876543210" }          // optional, E.164; lets the demo show repeat-caller memory and the SMS flow
```
`201` response:
```json
{ "call_id": "call-voice-3fa9c21b07", "greeting_text": "Namaste! AKGEC helpline mein aapka swagat hai ...", "lang": "hinglish",
  "kb_version": "a1b2c3d4e5f6", "interruptable": false, "idle_timeout_s": 120, "max_text_chars": 600 }
```
The runtime also announces the call to ops (`POST /v1/calls`, channel `web`). The page speaks `greeting_text` itself, then starts listening.
`lang` is one of `en`, `hi`, `hinglish` (use it to pick the speech voice / `lang` code).

### 2. Send a turn — `POST /api/voice/calls/{call_id}/turns`
Request:
```json
{ "text": "meri fees ki last date kya hai" }       // 1..600 chars (the final transcript, after the page decided the caller finished)
```
**Default response: Server-Sent Events** (`Content-Type: text/event-stream`). Use `fetch()` + a stream reader (EventSource cannot POST).
Events, in this order: one `meta`, zero or more `speech` / `action` (interleaved in the order they happen), then one `done`; or an `error` instead of `done`.
```
event: meta
data: {"turn_index":1,"path":"faq","tier":"cache","lang":"hinglish","interruptable":true}

event: speech
data: {"index":0,"text":"Fee ki last date 15 July hai."}

event: speech
data: {"index":1,"text":"Kya main aapko SMS bhej dun?"}

event: done
data: {"turn_index":1,"path":"faq","tier":"cache","lang":"hinglish","rule_id":null,"priority":null,"ticket_ids":[],"ended":false,"latency_ms":412}
```
- **`speech`**: one sentence of reply text to speak, in order (`index` counts from 0). Speak the first one as soon as it arrives; queue the rest. Ticket ids are already substituted and written ready to be read aloud ("E X one zero four two"). Never concatenate or re-order.
- **`action`** (informational; the runtime already executed it): `{"kind":"create_ticket","ticket_id":"EX-1042","type":"URGENT","priority":"P1","department":"exams"}`,
  `{"kind":"transfer","department":"exams","simulated":true}` (web demo: no real phone is dialled), `{"kind":"send_sms","template_id":"ticket_created"}`, `log_gap`, `schedule_callback`, `end_call`. Show them in the UI if wanted; do not speak them.
- **`meta.interruptable`**: `false` for emergency / ticket turns. While it is `false`, do NOT barge in or cancel; let the page finish speaking. The server also ignores a cancel for such a turn (CR-8) and keeps running the ticket, alert and transfer even if the browser disconnects.
- **`done.ended`**: `true` when the Brain ended the call (for example after a goodbye). The runtime then closes the call in ops by itself; the page should stop listening (calling `/end` afterwards is still fine, it returns 404 once ended).
- **`done.ticket_ids`**: tickets created during this turn.
- `path` ∈ `safety | flow | faq | llm | chitchat | fallback ...` (informational), `tier` the answer tier (informational), `lang` language of this reply.

**JSON mode (no streaming):** `POST .../turns?stream=false` (or send `Accept: application/json`) returns one object after the whole turn:
```json
{ "call_id":"call-voice-3fa9c21b07", "turn_index":1, "reply_text":"Fee ki last date 15 July hai. Kya main aapko SMS bhej dun?",
  "sentences":["Fee ki last date 15 July hai.","Kya main aapko SMS bhej dun?"], "lang":"hinglish", "path":"faq", "tier":"cache", "rule_id":null,
  "priority":null, "interruptable":true, "actions":[], "ticket_ids":[], "ended":false }
```
Streaming and JSON carry the same sentences; streaming only delivers them earlier. If the stream breaks mid-turn, do not replay the same text: start a new turn.

### 3. Cancel the turn being spoken (barge-in) — `POST /api/voice/calls/{call_id}/cancel`
Call this when the caller starts talking while the page is speaking. `200`:
```json
{ "call_id": "call-voice-3fa9c21b07", "cancelled": true }
{ "call_id": "...", "cancelled": false, "reason": "no_turn_in_progress" }     // nothing running (e.g. the stream already finished): normal, ignore it
{ "call_id": "...", "cancelled": false, "reason": "not_interruptable" }       // emergency / ticket turn: keep speaking
```
Also: sending a new turn while the previous one is still running cancels the previous one if it is interruptable; if it is not, the new turn is refused with `409 conflict` (`retryable: true`). Closing the stream (abort the fetch) also cancels an interruptable turn.
After a cancel the page stops speaking; nothing the cancelled turn has not yet sent is created or recorded (an interrupted turn is not posted to ops).

### 4. End the call — `POST /api/voice/calls/{call_id}/end`
No body. `200`:
```json
{ "status": "ended", "call_id": "call-voice-3fa9c21b07", "outcome": "answered", "ai_resolved": true, "turns_count": 4 }
```
This waits for background SMS work, ends the Brain session and posts **`POST /v1/calls/{id}/end`** to ops (`outcome` ∈ `answered | escalated | transferred | emergency | abandoned`, `ai_resolved` and `turns_count`), so the call counts as "Answered by AI" when it was resolved without a ticket or transfer.
A call with no turns ends as `abandoned`. A second `/end` for the same call returns `404 not_found` and posts nothing to ops. Call `/end` on hang-up, tab close (`navigator.sendBeacon` is allowed: no body, no custom headers needed when no bearer key is set) and on page unload.

### 5. Limits — `GET /api/voice/config`
```json
{ "api_version": 1, "contract_version": "1.0.0", "streaming": true, "idle_timeout_s": 120, "max_text_chars": 600, "caller_number_allowed": true }
```

## Server-side rules
- **Idle timeout:** a call with no turn for `idle_timeout_s` (`VOICE_WEB_IDLE_TIMEOUT_S`, default 120) is ended by the server and posted to ops (`abandoned` if there were no turns). Later calls with that id return `404 not_found`.
- **Capacity:** at most `VOICE_WEB_MAX_CALLS` (default 20) open calls; the next `POST /calls` gets `429 rate_limited` (`retryable: true`).
- **Shutdown:** every open call is ended and posted to ops.
- `caller_number` is accepted only when `VOICE_WEB_ALLOW_CALLER_NUMBER` is true (default true for the demo); otherwise `422`.

## Errors
Every non-2xx response (and an `error` SSE event's data) uses the contract's error envelope:
```json
{ "error": { "code": "not_found", "message": "Unknown or ended call.", "retryable": false, "details": {} } }
```
| HTTP | `code` | When |
|---|---|---|
| 401 | `unauthorized` | bearer key configured and missing/wrong |
| 404 | `not_found` | unknown, ended or already finalized call |
| 409 | `conflict` | previous turn cannot be interrupted yet (retry shortly); cancelled turn in JSON mode |
| 422 | `validation_error` | empty/oversized `text`, bad `caller_number`, malformed body (`details.fields` lists the fields) |
| 429 | `rate_limited` | too many open calls (retry later) |
| 500 | `internal` | the Brain failed (`retryable: true`); the call stays open, the page may speak a short apology and try again |

In the SSE stream a failure is reported as `event: error` with `{"code":"internal","message":"...","retryable":true}` and **no `done`**; treat the turn as failed.
A cancelled turn ends its stream with `event: error` / `code: "conflict"` / `"The turn was cancelled (barge-in)."` — that is the expected end of a barge-in, not a fault.

## Client checklist
1. `POST /calls` → speak `greeting_text`.
2. On each final transcript: `POST /turns` (streaming); speak `speech` events in order as they arrive.
3. If the caller talks while you speak and `meta.interruptable` is `true`: stop speaking, `POST /cancel` (or abort the stream), send the new transcript as the next turn.
4. If `done.ended` or the caller hangs up: stop listening; `POST /end` (the 404 after a Brain-ended call is fine).
5. Tab close: `POST /end` via `sendBeacon` (also covered by the server idle timeout).

## Compatibility with the first voice-web version (`/api/test/*`)
Kept working so the page can switch over without a rush: CORS (same allow-list) on `/api/test/*` and `/healthz`; `POST /api/test/turn` now also returns `ticket_id` (string or null); `POST /api/test/end` ends the call in ops and answers `{"ok":true,"status":"ended","outcome":"..."}` (`404 not_found` for an unknown or already-ended call). New work should use `/api/voice/*` (streaming, cancel, idle timeout, error envelope).
