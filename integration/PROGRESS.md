# Integration progress (newest first)

| Time (IST) | Step | Result | Evidence |
|---|---|---|---|
| 2026-11-02 evening | STEP 1: keys inventory | **DONE** (PASS) | `integration/NEEDED_KEYS.md`: 10 credentials, REQUIRED vs OPTIONAL, public URL needs, and 4 code risks in voice-runtime (non-standard RTC token, join payload shape, no ASR language, silent failure). Provider facts marked verify where only snippets were found. |
| 2026-11-02 afternoon | Pre-work: full-system validation on a local stack | PASS except C-1 | `integration/INTEGRATION_REPORT.md`: 35/35 scenario checks (a-i), J6 via direct reload, duplicate/ops-down drills; Publish button blocked by C-1 (`FINDINGS_FOR_C.md`). Suites: ops 550/2 skipped, brain 505+10, runtime 16, A's e2e 9, smoke 43, journeys 30, dashboard 239. |

Next: STEP 2 (live LLM across the stack, already measured at Brain level: 42/44 answerable, 24/24 abstain, p50 627 ms on gpt-oss-20b) once the shared keys arrive; STEP 3 needs the Agora, STT and TTS keys above.
