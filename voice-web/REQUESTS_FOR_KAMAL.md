# Requests for Kamal (voice-runtime). Nothing in `voice-runtime/**` was edited.

**Status after `integration/VOICE_WEB_API.md`:** R-1 (CORS), R-2 (end), R-3 (streaming) and R-4 (`ticket_id`) are covered by `/api/voice/*` and the `/api/test/*` compatibility notes; the page now uses `/api/voice/*`. Still open: **R-5** (tunnel URL, privately) and the points at the end of this file.

| # | Need | Why | Exact ask |
|---|---|---|---|
| **R-1 (blocks use from a tunnel)** | CORS on the runtime | The page runs from a different origin (the mock/page server, or any static host) than `http://localhost:8000` or the tunnel; browsers block the `fetch` without CORS headers. Preflight (`OPTIONS`) is sent because the body is JSON. | `CORSMiddleware(allow_origins=["*"] or the page origin, allow_methods=["GET","POST","OPTIONS"], allow_headers=["Content-Type"])` on `/api/test/*` and `/healthz`. Alternative that needs no code: serve the page itself from the runtime (copy `voice-web/` into `voice-runtime/resolvix_runtime/static/` and add a route), then everything is same-origin. |
| **R-2 (blocks "Answered by AI")** | An end endpoint for typed calls | The runtime posts `CallEnd` to ops only through the Agora shim (`session.is_ended`, `shim.py:~183`). Typed calls are never ended, so ops keeps them in "Active calls" and "Answered by AI" stays 0 (your A-2). | `POST /api/test/end` `{"call_id": "<id>"}`: look up the session, `outcome = session.derive_outcome()`, `ops_client.post_call_end(call_id, CallEnd(...turns_count=session.turn_count))`, `brain.end_call(call_id)`, drop the session; answer `{"ok":true,"outcome":"..."}` (404 for an unknown call). The page already calls it (`voice-web/src/api.js`). Also end the call when the brain emits an `end_call` action. |
| R-3 (optional, latency) | Stream the turn | Today the answer arrives only after the whole turn. Speaking the first sentence early needs a stream. | e.g. `POST /api/test/turn?stream=1` returning SSE or NDJSON with one `speech` event per line as it is produced, then `done`. The page's sentence queue already speaks sentence by sentence; only `sendTurn` needs a reader. |
| R-4 (nice to have) | `ticket_id` field in the turn response | The page extracts it from the text with a regex. | Add `"ticket_id": "EX-1042"` (or null) to `TestTurnResponse` when a ticket was created this turn. |
| R-5 | The tunnel / LAN URL and whether it is HTTPS | Chrome speech recognition needs a secure context: `http://localhost` is fine, a plain-`http` LAN address is not for the microphone. | Give the `https://` tunnel URL (cloudflared) if the page is opened on another machine; same-origin `localhost` is enough on one machine. |

Also useful, not blocking: have `/api/test/turn` accept an optional `lang` hint so the recognised language (hi-IN / en-IN) reaches the brain.

## New, after the switch to `/api/voice/*`
- Add the page origin to `VOICE_WEB_CORS_ORIGINS` (default is only `:5174`): `http://localhost:8200` for the mock/page server, the tunnel origin, or serve `voice-web/` at `/voice/` (then same origin).
- Keep `VOICE_WEB_API_KEY` unset for the demo: the page sends no bearer token yet (say if you want it; it is a one-line header).
- Please confirm the greeting is meant to be spoken by the page (`greeting_text`): it is spoken when the call is started with the mic button.
