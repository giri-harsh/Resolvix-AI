# Outbound phone calling API (v0, mock-only build)

Served by `voice-runtime` (`resolvix_runtime/outbound/`), contract for Member C / Harsh to call from Ops. **Nothing in this build can place a real call:**
the only provider is an in-memory mock, the real Exotel provider is a stub that refuses every request, and the feature is off unless `OUTBOUND_ENABLED=true`.
Do not describe this as working phone calling until a real, explicitly approved test call has succeeded (see OUTBOUND_RUNBOOK.md).

## Who calls what
```
Dashboard (browser) --cookie--> Ops API (authenticates the user, checks role, stores history) --X-Service-Key + user headers--> voice-runtime
Exotel (internet) --> public edge --> voice-runtime  POST /v1/webhooks/exotel/{token}   and   WS /ws/exotel/{token}
```
- The control endpoints below must **not** be reachable from the internet and must **never** be called from browser JavaScript.
- Only the two Exotel-facing paths are meant for a public edge (the allow-list proxy needs a WebSocket relay for that: not part of this build).

## Authentication (control endpoints)
| Header | Meaning |
|---|---|
| `X-Service-Key` | the runtime's `SERVICE_KEY` (constant-time compare; keys shorter than 16 chars or `change-me` are refused with 500) |
| `X-On-Behalf-Of` | email of the dashboard user Ops authenticated |
| `X-User-Role` | `admin` or `staff` (anything else: 403). `staff` sees and cancels only their own calls; `admin` sees all |
| `Idempotency-Key` | required on POST create, 8-128 chars `[A-Za-z0-9._:-]`, scoped per user |

## Endpoints
### `POST /v1/outbound-calls`
```json
{ "to": "+919876543210", "lang": "hi|en|hinglish",
  "consent": { "confirmed": true, "basis": "customer_requested|existing_relationship|opt_in", "note": "optional, max 200 chars" } }
```
Unknown fields are rejected (422). There is deliberately **no** `purpose` / category / hours-override field: calling-hour and limit treatment is decided by the server.
`202` (new) or `200` (idempotent replay of the same key and body): the call object below, `Location: /v1/outbound-calls/{call_id}`.

### `GET /v1/outbound-calls/{call_id}` and `GET /v1/outbound-calls?limit=&before=&status=`
The call object, or `{ "items": [call...], "next_before": <epoch float or null> }` (newest first, `limit` 1-100, pass `next_before` as `before`).

### `POST /v1/outbound-calls/{call_id}/cancel`  (`202`, idempotent)
- never dialled (`queued`): becomes `cancelled` at once;
- live call: records `cancel_requested: true`, closes our audio stream and asks the provider to hang up. **The status does not change until the carrier reports, the provider confirms a hang-up, or the local watchdog gives up** (then `hangup_confirmed: false`). Closing the WebSocket is not assumed to end a phone call.

### `POST /v1/webhooks/exotel/{token}`  (Exotel only)
JSON or `application/x-www-form-urlencoded` (multipart is not accepted yet). Auth: the unguessable per-call token in the URL **and** `CallSid` equal to the call's provider Sid. Exotel callbacks carry no signature in their docs. Always `200 {"ok":true,"applied":bool}` for a valid call (duplicates and out-of-order updates are ignored, never errors).

### `WS /ws/exotel/{token}`  (Exotel only)
Exotel Voicebot stream (8 kHz PCM16 mono, base64). The token is single use once `start` is accepted, expires after `OUTBOUND_TOKEN_TTL_S`, and `start.call_sid` must equal the stored provider Sid. Protocol details live in `bridge.py::ExotelProtocol` (UNVERIFIED against a live call).

## Call object
```json
{ "call_id": "call-out-3fa9c21b07de", "status": "dialing", "destination_masked": "+91*******210", "requested_by": "admin@x.edu", "lang": "hi",
  "created_at": "...Z", "dialing_at": "...Z", "answered_at": null, "connected_at": null, "ended_at": null, "duration_s": null,
  "outcome": null, "failure_reason": null, "provider": { "name": "mock", "sid": "MOCKSID0001" },
  "cancel_requested": false, "hangup_confirmed": false, "cost": null }
```
Full numbers, tokens and the consent note are never returned. `cost` is only filled if the carrier supplies it (never estimated).

## Status lifecycle
`queued` -> `dialing` -> `answered` -> `connected` -> `ended`; terminal results `failed`, `no_answer`, `busy`, `cancelled`. Illegal or late transitions are ignored.
There is no `ringing` (Exotel's callback payload has no ringing event). `connected` means the audio stream started; `answered` is a carrier report.

## Errors (`ErrorEnvelope`, code values are the frozen contract's)
| HTTP | `error.code` | details.reason (examples) |
|---|---|---|
| 401 | `unauthorized` | missing/invalid service key, missing user |
| 403 | `unauthorized` | `role_not_allowed`, `consent_required`, `country_not_allowed`, `emergency_number`, `number_type_not_allowed`, `blocked_number`, `outside_calling_hours`, `sid_mismatch`, `outbound_disabled` |
| 404 | `not_found` | unknown call/callback (staff asking for another user's call also gets 404) |
| 409 | `conflict` | `duplicate_call`, `idempotency_key_reuse` |
| 422 | `validation_error` | bad E.164 / body / Idempotency-Key / status filter |
| 429 | `rate_limited` (retryable, `Retry-After`) | `per_user_hour`, `per_user_day`, `per_destination_day`, `max_concurrent`, `daily_cap` |
| 500 | `internal` | server key not configured |
(The frozen `ErrorBody` has no `forbidden` code, so 403s use `unauthorized` with the HTTP status and `details.reason`.)

## Rules enforced server-side
E.164 only; allowed countries (default `+91`, Indian mobile numbers only); emergency and short codes never; caller ID and block list never; explicit consent on every request; India calling hours 09:00-21:00 IST (not caller-selectable);
per user 10/h and 30/day, per destination 2/day, 3 concurrent, daily call-time cap; 60 s duplicate lock; hard maximum call duration (default 180 s, sent to Exotel as `TimeLimit` and enforced by a watchdog); no bulk dialing.

## Database (SQLite, `OUTBOUND_DB_PATH`, default `integration/.run/outbound.db`, git-ignored)
`outbound_calls`: call_id, requested_by, requester_role, idempotency_key, request_fp, dest_masked, dest_hash (keyed), country_code, lang, consent_basis/note/at, status and per-status timestamps, duration_s, failure_reason, outcome, provider, provider_sid, stream_sid, ws_token_hash + expiry + ws_started, cb_token_hash, cancel_requested_at/reason, hangup_confirmed, ops_call_id.
`outbound_audit`: ts, call_id, event, actor, detail (JSON). Stored: masked numbers, hashes, provider ids, reasons. Never stored: audio, transcripts, credentials, full numbers, raw tokens.
Consent free-text notes are blanked after `OUTBOUND_CONSENT_RETENTION_DAYS`.

## What Ops / the dashboard still needs to build (Member C / Harsh, not done here)
An Outbound Calls page; a role check before calling these endpoints; the call-confirmation dialog showing the destination; showing `status`, `cancel_requested`/`hangup_confirmed` honestly; history from `GET /v1/outbound-calls`.
When a call connects/ends the runtime already posts the normal call start/end records to Ops with `channel: "pstn"`.
