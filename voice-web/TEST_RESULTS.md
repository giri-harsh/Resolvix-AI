# Test results

## 1. Unit tests: `node --test test/*.test.js` -> **53 passed, 0 failed** (Node 24). 28 original, 5 for the unbound-API bug (real globals, recognition errors), 20 for the streaming API: SSE parser (split chunks, CRLF, Hindi bytes), accumulator, stream vs mock under strict globals, abort/barge-in (interruptable and not), error envelopes, fallback decision and fallback runtime, sentence queue
Sentence splitter (danda, decimals, abbreviations), ticket-id speller, ticket-id extraction, voice choice and Hinglish override, idle timer
(fake clock: fires once, reset, pause/resume, stop), barge-in controller (cancels speech, aborts the request in flight, invalidates the old turn,
stale answers ignored), turn client against the mock server (first turn starts a call, ids reused, abort -> `AbortError`, unreachable runtime and HTTP
errors, end call, health), mock reply shapes.

## 2. Manual run against the mock in the built-in Chromium browser (typed box; same code path as speech)
| # | Check | Result |
|---|---|---|
| 1 | Page loads from the mock; status pill "runtime connected" | PASS |
| 2 | Hindi FAQ ("fees kab tak jama karni hai") -> faq/confident, Hindi reply | PASS (reply rendered; first-audio 648 ms after the typed submit in the run where it was not interrupted) |
| 3 | English FAQ (admission documents) -> faq/confident, lang en | PASS |
| 4 | Unknown question (bus route) -> llm/unknown, ticket `TR-1043`, actions create_ticket + log_gap | PASS |
| 5 | Exam P1 -> escalation/escalate, P1 tag, ticket `EX-1042`, create_ticket + transfer, "transfer requested (simulated)" line | PASS |
| 6 | Emergency -> safety/escalate, ticket `EM-1045` | PASS |
| 7 | Human request -> ticket `HD-1044` | PASS |
| 8 | Complaint -> slot_filling/escalate, ticket `HS-1046` | PASS |
| 9 | Speech out sentence by sentence; ticket spelled "Ticket number E X, 1 0 4 7." | PASS (captured through a `speechSynthesis.speak` hook) |
| 10 | Barge-in: sending a second message while the first was being processed/spoken cancelled the speech and the in-flight request; only the newest answer appeared, "interrupted (typed message)" logged | PASS |
| 11 | End call: "call ended (End call pressed)", End button disabled, call id cleared; the mock recorded the end | PASS |
| 12 | Timings panel filled: end of speech -> reply 259-267 ms (mock latency 250 ms), -> first audio 648-702 ms | PASS |
| 13 | Voices on this PC: en-US and en-IN only, no hi-IN; the page used en-IN for Hindi/Hinglish replies as designed | PASS (fallback) |

**Not exercised here:** real microphone recognition (the built-in browser has no microphone), the 60 s idle end in real time (covered by the fake-clock unit test),
Hindi voice quality (no hi-IN voice installed), continuous-mode restarts over a long session. These need a person with Chrome and a microphone.

## 2b. Streaming API against the streaming mock (built-in Chromium, typed box)
| Check | Result |
|---|---|
| auto mode starts `/api/voice/calls`, sentences spoken as they arrive, ticket id spelled by the runtime | PASS |
| barge-in with a second typed message: stream aborted, third sentence never shown or spoken, "interrupted" logged, no console errors | PASS |
| runtime without `/api/voice` (404): "streaming API unavailable ... using /api/test/*" in Raw events, reply works, call id `call-test-...` | PASS |
| end-of-speech -> first reply text / first audio, same question, mock latency 250 ms + 200 ms per extra sentence (2-sentence reply) | streaming: 268 ms / 434 ms; /api/test: 474 ms / 732 ms. The very first utterance after page load adds about 300 ms (voice start-up): 284 / 745 ms |

## 3. Against Kamal's runtime
**Not run yet.** It needs his URL (REQUESTS_FOR_KAMAL R-5) and CORS (R-1); the end-call endpoint (R-2) is needed for "Answered by AI" to increment.
Checklist to run when he is ready (record PASS/FAIL here):
| Scenario | Say | Expect | Result |
|---|---|---|---|
| FAQ Hindi | "fees kab tak jama karni hai" | spoken answer with the source | |
| FAQ English | "What documents are needed for admission?" | spoken answer | |
| Unknown | "What is the bus route to Noida sector 62?" | ticket id spoken, gap in the ops dashboard | |
| Exam P1 | "Exam 15 minute mein hai, room nahi mil raha" | P1 ticket, alert.p1 banner on the ops dashboard | |
| Emergency | "ambulance chahiye" | safety script, P1 ticket | |
| Human request | "mujhe kisi insaan se baat karni hai" | ticket + transfer/callback line | |
| Complaint | "hostel mess ki shikayat hai" | complaint ticket | |
| End of call | press End call | call ends in ops, "Answered by AI" increments (needs R-2) | |
