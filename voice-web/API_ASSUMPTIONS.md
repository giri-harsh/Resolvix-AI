# API used by the page

Source of truth: `integration/VOICE_WEB_API.md` on `origin/member-b-integration` (Kamal, v1, `voice-runtime/resolvix_runtime/voice_api.py`).
If it changes, it wins over this file. Nothing in `voice-runtime/**` or `integration/**` is edited from here.

## Streaming API (default): `/api/voice/*`
| Purpose | Call | How the page uses it |
|---|---|---|
| Start | `POST /api/voice/calls` (empty body) -> `201 {call_id, greeting_text, lang, idle_timeout_s, max_text_chars}` | on the first mic press or the first sent message; the greeting is spoken when the call was started with the mic |
| Turn | `POST /api/voice/calls/{id}/turns` `{text}` -> `text/event-stream` | `fetch()` + stream reader (`src/stream.js`, `SSEParser`); `meta`, `speech {index,text}` (spoken at once, in order, one queue), `action {kind,...}`, `done {priority,ticket_ids,ended,...}`, or `error` |
| Barge-in | abort the fetch | Closing the stream cancels an interruptable turn on the server (doc section 3). The page does **not** also `POST /cancel`: a cancel that overtakes the next turn could cancel the new turn. Nothing of the cancelled turn is shown or spoken afterwards |
| End | `POST /api/voice/calls/{id}/end` | End button, 60 s idle, `done.ended` (after the last sentence is spoken), tab close via `sendBeacon`. `404` = already ended = fine |
| Limits | `GET /api/voice/config` | read by `voiceConfig()` (not needed to run) |
| Health pill | `GET /healthz` | unchanged |

Rules followed from the doc:
- Speak `speech` events as they arrive, never re-ordered or merged. Ticket ids arrive already written for speech ("E X one zero four two"), so the page does **not** run its own ticket-id speller on this path.
- `meta.interruptable === false` (emergency / ticket turns): barge-in (voice, mic press, typed message) is ignored with a note in Raw events; a transcript that arrives meanwhile is queued and sent when the bot has finished.
- `error` with `code: "conflict"` ends a barge-in: expected, no message. Any other `error` event is shown as the failure of the turn (no `done`).
- A broken stream is not replayed; the user speaks again.
- `404 not_found` on a turn = the server closed the call (idle timeout / brain ended it): the page resets and the next sentence starts a new call.

## Fallback: `/api/test/*` (the stock runtime typed-turn API)
Used automatically when the **start call** of the streaming API fails with 404/405/501, a 5xx or "runtime unreachable"
(an older runtime, or a missing route), and when a streamed turn gets a 404/405 without an error envelope.
The **API** selector can force either path (`streaming only`, `fallback only`), which is how both are timed.
| Purpose | Call |
|---|---|
| Turn | `POST /api/test/turn {call_id|null, text}` -> `{call_id, spoken_text, ticket_id?, events[]}`, one JSON answer after the whole turn |
| End | `POST /api/test/end {call_id}` (404/405 = unsupported on older runtimes: the page says so) |
`ticket_id` in the answer is used when present (R-4), otherwise the regex on the spoken text. This path speaks the whole reply at once, split into sentences, and uses the page's ticket-id speller.

## Behaviour notes
- Language of a reply = `meta.lang` (`hi`, `en`, `hinglish`); Devanagari text always uses the Hindi voice family.
- Timings: end of speech -> first reply text, and end of speech -> first audio, shown separately for the streaming path and for `/api/test`.
- CORS: `VOICE_WEB_CORS_ORIGINS` on the runtime must contain the page origin, unless the page is served from the runtime under `/voice/`.
  No cookies are sent. A bearer key (`VOICE_WEB_API_KEY`) is **not** supported by the page yet; it is not set for the demo.
