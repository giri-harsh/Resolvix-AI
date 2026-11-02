# Integration progress (newest first)

### 2026-11-03 00:35 - Milestone 1 step 1 + eval runner (PASS), waiting on kb-data and Agora keys
- Merged origin/member-c-ops (8c111da: voice-web streams via /api/voice). Runtime serves /voice/; the page (streaming mode) answered a typed turn end to end; /api/test/end and ticket_id in the turn response were verified earlier (d6db5a7, 13a8bee).
- integration/top40_eval_runner.py written and smoke-tested on a 3-utterance sample (PASS / ESCALATED / LLM classes, per-language rates, TOP40_RESULTS.md). Needs kb-data/top40_eval.jsonl (not on member-c-ops yet). must_contain should be language-neutral (figures, names): a Hinglish answer says "10 baje", not "10 pm".
- BLOCKED (human): (a) kb-data/top40_kb.jsonl + top40_eval.jsonl from Harsh; (b) AGORA_CUSTOMER_ID / AGORA_CUSTOMER_SECRET are not in integration/.env (the copy in the second worktree has a 6-character secret; D:\hackathon\tally\.env is another project and was not used).

| Time (IST) | Step | Result | Evidence |
|---|---|---|---|
| 2026-11-02 night | Agora credentials check (App ID + Certificate) | **PASS** | `.env` values present and plausible (32 hex each). Built a real AccessToken2 (`007`) with Agora's official `AgoraIO/Tools` builder and joined a channel from the browser with `AgoraRTC_N.js`: `CONNECTING -> CONNECTED`, "RESULT: PASS - connected with a 007 token". So the App ID, certificate and token approach are valid; the fix for A-6 is to use that builder. Not yet possible: the AI agent join (needs Customer ID/Secret + Conversational AI enabled) and the public tunnel. Reproducible with `integration/agora_join_test/`. |
| 2026-11-02 night | STEP 2: live LLM through the full stack | **PASS** | Stack started with `-LlmMode live` (`/healthz` brain `llm_mode=live`, model `gpt-oss-20b`). `e2e_scenarios.py`: **32/32 PASS** (a-f, h, i; g skipped: it needs the mock LLM timeout trigger). Turn wall time via the runtime (ms): FAQ 450 (n=1), LLM answer 918 (n=1), escalation p50 1,363 / p95 1,539 (n=8), slot filling p50 167 (n=7), emergency 2,517 (n=1, ticket + 2 SMS + transfer awaited in sequence). Brain eval live on the sample KB: answerable **44/45 (98%)**, abstain **14/15 (93%)**, urgency 10/10, safety 38/38, human 10/10, fabricated figures 0; LLM calls n=66 all ok on 20b, total p50 700 / p95 1,347 / p99 2,805 ms, TTFT p50 514 / p95 1,075 ms, prompt cache hit on 64/66 calls (89% of prompt tokens). On the AKGEC draft KB (temporary approved copy): answerable 42/44, abstain 24/24. Misses: "backlog exam ki date kab hai" answered with the regular exam date (abstain set), "exam ki date sheet..." escalated although answerable. |
| 2026-11-02 night | Voice-runtime check after A's `176891c` | **BLOCKED (A)** | Merged `origin/member-a-voice-runtime`. 176891c changes only `brain_loader.py` (loud failure, tests) and docs; `agora_control.py` is unchanged: RTC token still hand-rolled (A-6), join payload still `parameters` not `properties` (A-7), no ASR language / voice id `"default"` (A-8), join failure is still only a warning (A-9). All written to `FINDINGS_FOR_A.md` with file:line and Agora doc links. Waiting for A. |
| 2026-11-02 night | Agora-managed ASR/TTS instead of Deepgram/ElevenLabs? | **Partly yes** | Managed mode (`credential_mode: "managed"`): Deepgram nova-2/nova-3 ASR is managed, so the Deepgram key can likely be dropped; managed TTS is MiniMax, ElevenLabs is key-only. Hindi quality under managed mode is undocumented: test first. Details in `NEEDED_KEYS.md`. |
| 2026-11-02 night | Tunnel | **NOT STARTED** | `cloudflared` not installed; install needs approval. Steps recorded in `VOICE_PLAN.md`. |
| 2026-11-02 evening | STEP 1: keys inventory | **DONE** (PASS) | `integration/NEEDED_KEYS.md`: 10 credentials, REQUIRED vs OPTIONAL, public URL needs, and 4 code risks in voice-runtime (non-standard RTC token, join payload shape, no ASR language, silent failure). Provider facts marked verify where only snippets were found. |
| 2026-11-02 afternoon | Pre-work: full-system validation on a local stack | PASS except C-1 | `integration/INTEGRATION_REPORT.md`: 35/35 scenario checks (a-i), J6 via direct reload, duplicate/ops-down drills; Publish button blocked by C-1 (`FINDINGS_FOR_C.md`). Suites: ops 550/2 skipped, brain 505+10, runtime 16, A's e2e 9, smoke 43, journeys 30, dashboard 239. |

Next: STEP 3 needs A to fix A-6..A-9, the Agora (and STT/TTS or managed mode) keys, and the tunnel. Meanwhile: C-1 retest when Harsh pushes, CR-7 option 2 / decision 27 (already in `brain/**`, see tests), DEMO_SCRIPT.md.

## Voice-web API (Plan B) â€” step 1 done
- `integration/VOICE_WEB_API.md` is the contract; implemented in `voice-runtime/resolvix_runtime/voice_api.py` (mounted at `/api/voice`): start call, SSE turn stream (+ `?stream=false` JSON), cancel (CR-8 aware), end (posts `/v1/calls/{id}/end` to ops), config, server idle sweeper, optional bearer key, CORS from `VOICE_WEB_CORS_ORIGINS`.
- voice-web R-1 (CORS), R-2 (`/api/test/end` ends the call in ops), R-4 (`ticket_id` in turn response) handled on the legacy `/api/test/*` too. R-3 = streaming in `/api/voice`. R-5 waits for the tunnel.
- Tests: `voice-runtime/tests/test_voice_web_api.py` (12). Runtime suite 44 passed.
- Also fixed: OpsClient created a new HTTP client (SSL context, ~0.4 s) for every ops call; now one pooled client.

- voice-web served by the runtime at /voice/ (same origin, no CORS). R-2 (/api/test/end) was already done in 13a8bee. Tunnel (R-5) waits for approval; /api/voice and /voice must be added to the allow-list proxy first.

- Stack restarted from this worktree (SHIM_API_KEY rotated to a 43-char random key). Proxy is in voice-web mode (VOICE_PROXY_VOICE_WEB=1): only the /voice/ page, /api/voice/*, /api/test/{turn,end} and /healthz are forwarded; caller_number is stripped; 120 req/min. Quick cloudflared tunnel points at 127.0.0.1:8800 only (pid recorded, stop_all stops it). Checked through the public URL: page 200, call/turn/end work (turn 0.6 s, ops end posted), /api/sessions/web, /admin/kb/reload, /v1/calls, /docs and /voice/README.md all 404.

## Local voice-web checklist (real Brain, live Groq gpt-oss-20b, page at http://localhost:8000/voice/)
Run in the built-in Chromium browser by typing into the page's "or type a sentence" box (same code path as speech after the transcript). The microphone and
the spoken audio itself were NOT exercised (nobody can speak to the browser from this session): STT quality and TTS voice still need a human with a mic.
Timings are "end of input -> reply received / first audio" as the page shows them.

| Scenario | Result | Detail |
|---|---|---|
| FAQ hi ("meri fees ki last date kya hai") | PASS | llm/confident, 706 ms reply / 900 ms audio, B.Tech semester fee date. (One earlier cold run hit llm_timeout at ~2 s and gave the safe fallback + ticket.) |
| FAQ en ("what time does the hostel gate close") | PASS | llm/confident, 519 / 852 ms, "10 pm weekdays, 11 pm weekends" |
| Unknown ("bus route 12 timing") | PASS | escalation, 401 / 761 ms, offers ticket, then ticket TR-10xx created after "haan ticket bana do" (335 ms) |
| Exam P1, English | PASS | R-EXAM-60 P1, ticket + transfer(exam_cell), 830 ms |
| Exam P1, Hinglish ("exam 15 minute mein hai, hall ticket nahi ho raha") | FIXED, then PASS | was P2 R-DEADLINE-24H because "hall ticket" made the event an admit card; brain/urgency.py now lets an exam within 60 min outrank the admit-card wording. Now R-EXAM-60 P1; asks the paper name, then ticket + transfer (61 ms). Brain tests 516 passed |
| Emergency ("dost behosh ho gaya") | PASS | R-SAFETY P1, script first, ticket EM-xxxx + staff SMS + transfer in 37 ms, outcome emergency |
| Human request | PASS | ticket HD-xxxx, "office closed, staff will call", 287 / 451 ms |
| Complaint by voice (wifi) | PASS | slot filling (block/room, mobile number, readback), ticket HS-1020 + SMS after "haan sahi hai" |
| Ops side | PASS | tickets listed with priority and reason (EX P1, EM P1, HS P3); ended calls show outcome, ai_resolved and turns; "Answered by AI" = ended calls with ai_resolved true (answered), escalated/emergency/transferred are not counted. Verified through the ops API (the dashboard's source); the dashboard UI was not started in this run |

Findings: (1) the first LLM call after a restart or idle can take 2-3.4 s and sometimes times out (LLM_TTFT_TIMEOUT_S=3) -> fallback + ticket, so keep one warm-up turn before a demo;
(2) AKGEC KB drafts do not cover bonafide certificate or library timings, so those become tickets; (3) the page's Ticket field stays "-" until it reads ticket_id from the response (R-4 is in place).
