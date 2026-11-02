# API assumptions (derived from the runtime code)

`integration/VOICE_WEB_API.md` was **not** on `origin/member-b-integration` yet (commit bd6a6ba). This page uses what the runtime's own
typed test console uses: `voice-runtime/resolvix_runtime/app.py` (`/api/test/turn`, `/healthz`) and `static/test.html`.
When Kamal writes `VOICE_WEB_API.md`, compare it with this file; anything different wins.

## What the page calls
| Purpose | Call | Notes |
|---|---|---|
| Health pill | `GET {RUNTIME_URL}/healthz` | `status` is `ok` or `degraded`; polled every 8 s |
| Start a call | none: `POST /api/test/turn` with `call_id: null` on the first utterance | the runtime creates `call-test-xxxxxxxx`, calls the brain's `start_call` and posts `CallStart` to ops (channel `test`) |
| Send a turn | `POST /api/test/turn` `{"call_id": "<id or null>", "text": "<final transcript>"}` | one JSON response per turn, **not streamed** |
| End the call | `POST /api/test/end` `{"call_id": "<id>"}` | **does not exist in the stock runtime** (see REQUESTS_FOR_KAMAL R-2). The page tries it and says so when it gets 404/405. |

## Response of `/api/test/turn`
```json
{ "call_id": "call-test-1a2b3c4d",
  "spoken_text": "all speech pieces joined with spaces, ticket ids already substituted",
  "events": [
    {"type":"meta","path":"faq","tier":"confident","lang":"hi","interruptable":true},
    {"type":"speech","text":"..."},
    {"type":"action","action":{"type":"create_ticket", "...": "..."}},
    {"type":"done","summary":{"path":"faq","tier":"confident","lang":"hi","kb_ids":["FEE-001"],"priority":"P4", "...": "..."}} ] }
```
The page uses: `spoken_text` (split into sentences, each spoken), `events[meta].lang/path/tier/interruptable`,
`events[done].summary.priority/kb_ids`, `events[action].action.type` (`create_ticket`, `transfer`, `end_call`, `send_sms`, `log_gap`...).
The **ticket id** is read from `spoken_text` with `[A-Z]{2}-\d{4}` (the runtime substitutes `{{ticket_id}}` before returning), because the
response has no separate ticket field (R-4).

## Behaviour assumptions
- Language of the reply = `meta.lang` (`hi`, `en`, `hinglish`); Devanagari text always uses the Hindi voice family.
- `interruptable:false` (safety, ticket-id read-outs) is **shown but not enforced**: the page always lets the user barge in.
- No CORS headers on the stock runtime, so a browser page on another origin cannot call it (R-1). The mock server sends `*`.
- Barge-in cannot cancel work inside the runtime: aborting the `fetch` only stops waiting. A cancelled turn may still have created a ticket.
- Because the response is not streamed, speech starts only after the whole turn (including ticket creation and any LLM time) is done. The first-audio
  timing the page shows is therefore "end of speech -> reply received -> first audio", and streaming would only help if Kamal adds it (R-3).

## What Kamal must confirm or add
See `REQUESTS_FOR_KAMAL.md`: R-1 CORS, R-2 end-call endpoint, R-3 optional streaming, R-4 ticket id field, R-5 tunnel URL.
