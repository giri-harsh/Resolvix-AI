# Integration report: Resolvix (A voice runtime, B brain, C ops) on one machine, 2026-11-02

Workspace: git worktree `D:\hackathon\Resolvix-AI-integration-validation`, local branch `integration-validation`, based on `origin/integration` @ `4f28de6`
(already contains PR #9 Member A, PR #10 Member B, PR #11 Member C). Member B's newer, not-yet-merged work (Groq empty-answer handling, eval tweaks)
and the new `integration/` files were added on top, uncommitted until reviewed. Nothing was pushed.

## Environment
Windows 11, Python 3.12.6 (venv `C:\rx\vint`; Member C's pinned requirements.txt installed: fastapi 0.115.6, httpx 0.27.2, pytest 8.3.4), Node 24, npm 11.
Ports: ops 8100, voice runtime 8000, dashboard 5173 (no collisions). LLM: `mock` for the wiring runs, **live Groq** for the Brain evaluation. Real voice, Agora, ASR/TTS, Twilio: not available (see Walls).

## Test suites (all executed on this machine)
| Component | Command | Result |
|---|---|---|
| Contracts | `pytest contracts/tests` | 6 passed |
| A voice runtime | `pytest voice-runtime/tests` | 16 passed |
| A integration test | `pytest integration/e2e_test.py` against the running real-Brain stack | 9 passed |
| B brain offline | `pytest -m "not realembed"` | 505 passed, 0 failed, 0 skipped |
| B brain real embedding | `pytest -m realembed` | 10 passed |
| B eval | `eval --mock` (sample KB) / `eval --mock --kb-set akgec-draft` | all deterministic sets 100%; akgec-draft answerable 44/44, abstain 24/24 (retrieval proxies) |
| B live Groq | `eval --kb-set akgec-draft` with `LLM_MODE=live` | answerable 42/44, abstain 24/24, 0 fabricated figures (details in `brain/docs/LATENCY.md`) |
| C ops API | `pytest` in `ops/api` | 550 passed, 2 skipped |
| C smoke / journeys | `dev/smoke.py`, `dev/e2e_journeys.py` | 43 PASS / 30 PASS, exit 0 (they need `ops/api/.env`, copied from `.env.example`) |
| C dashboard | `npm run build`, `npm test` | build OK, 239 tests passed |

## How the system runs
`integration\run_all.ps1 -Python <python>` starts ops (seeded), the runtime with `BRAIN_IMPL=real` (the Brain fetched `GET /v1/kb/export` with the key and activated
12 entries, 2,876 tokens) and the dashboard; it fails if `/healthz` does not show the real Brain (`embedder: intfloat/multilingual-e5-small`). Verified in the run:
`runtime_log: "Initializing real resolvix_brain..."`, `kb activated version=99152f80995e entries=12`. Dashboard login as `admin@demo` showed 8 calls, two P1 tickets (EM-1003, EX-1002) on the live board and the active calls list.

## Scenarios (typed test console -> runtime -> real Brain -> ops), `integration/e2e_scenarios.py`: 35/35 checks passed on a clean start
| | Scenario | Result | Evidence |
|---|---|---|---|
| a | FAQ answer hi + en | PASS | "30 November" and "10 pm" spoken; `GET /v1/calls/{id}` has the turn (path faq / llm); no ticket |
| b | Unknown question | PASS | log_gap, `KNOWLEDGE_GAP` ticket (TR-...), 1 cluster in `/v1/gaps/clusters` |
| c | Exam in 15 min, blocked | PASS | `URGENT` P1 exam_cell (EX-...), `create_ticket` before `transfer`, `alert.p1` seen on `/v1/stream`. Note: "hall ticket" wording classifies as admit_card (P2) by the rules |
| d | Emergency | PASS | `EMERGENCY` P1 emergency, ticket before transfer, 2 `staff_alert` SMS (mock), `alert.p1` for that ticket, "112" script |
| e | Human request | PASS | `HUMAN_REQUEST` ticket; transfer or callback line |
| f | Complaint with read-back | PASS | `COMPLAINT` with `collected_fields` {category wifi, location "Block B room 214", callback number} |
| g | LLM failure | PASS | `SYSTEM_FAILURE` P3, turn summary path fallback, rule R-FALLBACK, callback line |
| h | SMS / CR-7 | PASS | one `ticket_created` SMS (mock); a second ticket in the same call created no second SMS and no SMS claim was spoken |
| i | Staff J5 | PASS | `hostel@demo` set in_progress -> exactly one `status_change` SMS (mock); repeat sends none; `exam@demo` gets 404 |
| j | Live KB publish (self-healing) | **FAIL via dashboard Publish, PASS via direct reload** | Publish returned an error because ops deadlocks on its own export (FINDINGS_FOR_C C-1). Workaround verified: approve -> `POST /admin/kb/reload` -> `KBVersionInfo {kb_version f77248fd334c, entry_count 13, faq_count 7}` -> the question is then answered from TRN-901 (path faq). Before: escalation. |
| k | Failure drills | PASS | Duplicate call/ticket/SMS deliveries: 201->200, 201->200 same id `created:false`, 202->200 original result, exactly one SMS event; wrong key 401. Ops stopped mid-call: answers continue, ticket requested and queued, "registered" line spoken (2 turns took 20.6 s); after ops restart the queued ticket arrived (HS-1002). |

## Real Brain, no MockBrain
Confirmed three ways: runtime log line, `/healthz` `brain.embedder`, and `kb_version` changing after a KB reload (a MockBrain has no KB). `BRAIN_IMPL=real` with a forced import failure raises
(PR #9 fix, verified).

## Brain (Member B) findings handled in `brain/**`
- `gpt-oss-120b` with `reasoning_effort=low` often returns no answer text (diagnosed with real requests; details in `brain/docs/LATENCY.md`). Fix: reasoning never counts as an answer, `empty`/`truncated` errors with token counts, primary tried second after 2 empties in a row, tolerant warm-up, fallback no longer blocked by the 1.5 s gate for answer-less replies. 17 new tests (`tests/test_llm_stream.py`).
- Live evaluation exposed a wrong FAQ-cache hit (founding year answered with the director entry): the director entry (flagged possibly outdated) is no longer a FAQ entry.

## Findings for the others
- C-1 (blocks the dashboard Publish button): ops holds its DB write transaction while waiting for the runtime, which calls `/v1/kb/export` back: `database is locked` -> 500 (kb_routes.py:177, publish.py:66, db.py:27). C-2 `turns_count` off by one (calls_routes.py:157). Details: `FINDINGS_FOR_C.md`.
- A-2 typed test calls are never ended (live board, "Answered by AI"), A-3 no caller number on the test channel, A-4 ~10 s stall per ticket when ops is down, A-5 simulated transfers show as `transfer_failed`. Details: `FINDINGS_FOR_A.md`.

## Walls (need someone with credentials)
- **Voice**: real microphone/speaker calls need Agora (App ID, certificate, customer ID and secret), an ASR key (Deepgram), a TTS key (ElevenLabs) and a public HTTPS URL for Agora to call the shim (Cloudflare Tunnel / ngrok). Not tested: the Agora shim was only exercised by Member A's unit tests.
- **Phone**: needs a telephony number/SIP (Agora telephony) and, for SMS to real phones, Twilio with DLT-registered sender and templates. Everything ran with `NOTIFY_MODE=mock` and `TELEPHONY_MODE=mock`.
- **Real AKGEC answers**: the AKGEC KB is 21 *draft* entries; Ops only exports approved ones, so staff must approve them (dashboard KB page) before the Brain can serve them. The runs above used Ops' seeded sample KB (12 entries) for the full-system scenarios; the AKGEC entries were evaluated at Brain level with a temporary approved copy.
- **Real college data**: fee notice, hostel notice, library timings, exam calendar, transport routes and helpdesk phone numbers are still missing, plus native Hindi review and staff review of the safety/urgency word lists.
- **Groq**: the `gpt-oss-120b` empty replies are a provider-side behaviour we can only work around.

## Demo script recommendation
Use: "B.Tech second year ki semester fee ki last date kya hai?" (FAQ), "What time does the hostel gate close?", "Mera exam 15 minute mein hai aur exam hall nahi mil raha" (P1),
"ek ladki behosh ho gayi hai" (emergency), "Hostel ka wifi do din se nahi chal raha" (complaint with read-back), "Noida sector 62 ke liye bus route kya hai?" (gap, then fix it live: approve an entry, reload). Avoid "fire safety course" style phrases (ordinary
questions with fire/attack words are excluded from the safety path, but a genuine "fire" statement is P1) and remember "fail hua toh mar jaunga" is P1 by design.

## Is it ready for Harsh's review?
The three components run together with the real Brain and the 35 wiring checks pass. Not ready to call "demo-complete" until C-1 is fixed (dashboard Publish) and, for a spoken demo, the voice credentials above exist.
