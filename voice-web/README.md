# Resolvix Voice (browser voice chat, Plan B)

Talk to the Resolvix bot by voice from Chrome. The page listens with the browser's speech recognition, sends each finished sentence to the
runtime's streaming voice API (`/api/voice/*`, one event per sentence) and speaks each sentence as it arrives with the browser's speech synthesis. If the runtime has no streaming API the page falls back to the typed-turn API (`/api/test/*`) by itself. No keys, no install, no build.

## Run it against the mock (no runtime needed)
```bash
cd voice-web
node mock_server/server.js          # serves the page and a fake runtime on http://localhost:8200
```
Open **http://localhost:8200/** in **Chrome**, press **Start talking**, allow the microphone, and say for example
"fees kab tak jama karni hai", "Exam 15 minute mein hai, room nahi mil raha", "mujhe kisi insaan se baat karni hai", "ambulance chahiye".
You can also type in the box: it takes the same path as speech.

## Run it against the real runtime (Kamal's machine or tunnel)
1. Kamal starts the stack (`integration\run_all.ps1`) and gives you the runtime URL: `http://localhost:8000` on his machine, or his `https://...` tunnel.
2. Serve the page: `cd voice-web && node mock_server/server.js 8200` (it also serves the files), open it, and put the runtime URL in **Runtime URL**
   (or open `http://localhost:8200/?runtime=https://his-tunnel.example`). The setting is remembered.
3. The runtime must allow cross-origin calls and should have an end-call endpoint: see **REQUESTS_FOR_KAMAL.md** (R-1, R-2).
   Until R-1 is done the status pill shows "runtime unreachable" for any other origin; the mock is unaffected.

### Served by the runtime (no CORS needed)
If Kamal serves the `voice-web/` files from the runtime under `/voice/` (note the trailing slash, the page uses relative paths), the page defaults to its own origin as the Runtime URL, so it also works through his tunnel without any setting.

## What it does
- **Mic button + continuous mode.** Language selector hi-IN / en-IN. Interim words show under the buttons, final sentences are sent.
- **Call lifecycle.** The call starts with the first sentence, ends on **End call**, after **60 s** without speech, or when the brain says `end_call`.
- **Speech out.** The reply is spoken sentence by sentence; Hindi/Hinglish replies prefer a hi-IN voice (selector: use an en-IN voice instead,
  which often reads romanised Hindi better), English replies an en-IN voice. Ticket ids are spelled: `EX-1042` -> "E X, 1 0 4 2".
- **Barge-in.** Speaking, pressing the mic or sending a typed message silences the bot at once and cancels the request in flight (the answer to
  a cancelled request is dropped). With "Barge-in while the bot speaks" off (no headphones) the mic is paused while the bot talks, so it does not hear itself.
- **Streaming.** The reply text appears sentence by sentence and each sentence is queued for speech as soon as it arrives. The **API** selector (auto / streaming only / fallback only) lets you force a path, which is how the two are compared.
- **Not-interruptable replies** (emergency, ticket read-outs; `meta.interruptable: false`): barge-in is ignored until the bot has finished; what you say meanwhile is queued.
- **Panels.** Conversation, path/tier, reply language, ticket id, actions, timings (end of speech -> first reply text; -> first audio for the streaming path and for `/api/test`, side by side), raw events + diagnostics (voice used, recognition errors, fallback decisions),
  connection pill (`/healthz`).

## Tests
```bash
cd voice-web
node --test test/*.test.js          # 53 tests (incl. real-globals tests with browser-like strict setTimeout/fetch): SSE parser, sentence queue, barge-in/abort, fallback, streaming against the mock, sentence splitter, ticket-id speller, voice choice, idle timer, barge-in, turn client against the mock
```
The manual checklist and its results are in **TEST_RESULTS.md**.

## Known limits
- **Chrome (or another Chromium browser) only.** Firefox has no `SpeechRecognition`; the typed box still works.
- Chrome's recognition sends audio to Google, so it needs internet; microphone access needs `http://localhost` or HTTPS.
- Voice quality depends on the voices installed in your browser: some PCs have no hi-IN voice (the page falls back to en-IN, then any voice).
- On the `/api/test` fallback the runtime answers once per turn, so the bot starts speaking after the whole turn is processed.
- Barge-in aborts the stream (the server cancels an interruptable turn); on the fallback path it only stops waiting and the turn may still have created a ticket.
- The page needs the runtime to allow its origin (`VOICE_WEB_CORS_ORIGINS`) or to serve it under `/voice/`; a bearer key (`VOICE_WEB_API_KEY`) is not supported yet.
