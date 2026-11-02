# Findings for Member C (ops). Nothing in `ops/**` was edited.

## C-1 (blocks the J6 demo): dashboard "Publish" fails with "database is locked" when the runtime calls `/v1/kb/export` back
- **Where:** `ops/api/resolvix_ops/routers/kb_routes.py:177` (`publish_kb`, `db: Session = Depends(get_db)`), `resolvix_ops/kb/publish.py:66-67` (synchronous `httpx.post` to the runtime), `resolvix_ops/db.py:23-27` (`BEGIN IMMEDIATE` on every transaction), export handler `kb_routes.py:161`.
- **Evidence:** ops started with `RUNTIME_MODE=live`, one uvicorn worker. Admin session `POST /v1/kb/publish` returned
  `422 {"error":{"code":"validation_error","message":"KB reload compilation failed: cannot fetch http://localhost:8100/v1/kb/export: HTTP 500", ...}}`.
  `integration/.run/ops.err.log`: `Unhandled OperationalError on GET /v1/kb/export` ... `db.py:27 begin_immediate` ... `sqlite3.OperationalError: database is locked [SQL: BEGIN IMMEDIATE]`.
  A plain `GET /v1/kb/export` with the service key right afterwards returns 200 (6 KB), so the export itself is fine.
- **Cause:** the publish request keeps its DB session/write transaction open while it waits for the runtime; the runtime (Brain) calls back into the same ops process, whose export handler needs its own `BEGIN IMMEDIATE`, and times out on the busy lock (5 s) with a 500.
- **Suggested fix:** do not hold the DB session across the HTTP call (resolve the principal, close/commit, then call `live_publish`, then reopen a session to write the audit and `kb_publishes` rows), and/or make `GET /v1/kb/export` a read-only (deferred) transaction.
- **Workaround used (verified):** after approving, trigger the reload directly: `POST http://localhost:8000/admin/kb/reload` with `X-Service-Key` and body `{"kind":"url","location":"http://localhost:8100/v1/kb/export"}` returned `KBVersionInfo` (kb_version changed 99152f80995e -> f77248fd334c, 13 entries) and the new entry was then answered. See `e2e_publish_and_drills.py --phase j-workaround`.

## C-2 (cosmetic): `calls.turns_count` is one too high
`routers/calls_routes.py:157` sets `turns_count = max(turns_count, turn_index + 1)` (assumes 0-based). The runtime and the Brain use 1-based turn indexes, so a call with 2 turns shows `turns_count: 3` (ops call row) while 2 turn rows are stored (`turn_index` 1 and 2). The contract allows `turn_index >= 0` (`models.py:164`), so either side may be right; please agree on the base.

## C-3 (observation, not a bug): dashboard "Answered by AI" stays 0
Typed-test calls are never ended (FINDINGS_FOR_A.md A-2), so `outcome` / `ai_resolved` are never set and every call stays in "Active calls".

## Verified OK (no action)
Idempotency (calls 201/200, tickets 201/200 with the original id, SMS 202/200 with the original result), 401 on a wrong key, 403 for the service key on publish, department-scoped staff (other department gets 404), one `status_change` SMS per change, two `staff_alert` SMS for an emergency, `alert.p1` on `/v1/stream`, gap clustering, `/v1/kb/export` excludes drafts. Own suites: pytest 550 passed / 2 skipped, smoke 43 PASS, e2e_journeys 30 PASS, dashboard build OK and 239 vitest tests passed.

## C-4 eval data: must_contain is written in English display form (kb-data/top40_eval.jsonl)
Evidence: the typed turn returns the TTS form of the answer: "1,42,156" is spoken "1 lakh 42 thousand 156 rupees", Hindi/Hinglish replies are the entry's Hindi answer
(Devanagari, names transliterated: "NEFT" -> "एनईएफटी", "Transport Form" -> "ट्रांसपोर्ट फॉर्म"). A literal match on must_contain therefore failed 33 of 60 correct answers (27/60 strict).
Workaround in integration/top40_eval_runner.py: must_contain also matches through the brain's own speech formatting, and a reply that is the expected entry's own reviewed
answer in the reply language counts as a pass ("entry verbatim", shown separately in TOP40_RESULTS.md). Suggested: keep must_contain as is, or add a language-neutral fact (digits only, e.g. "142156").

## C-5 eval expectations vs the safety rules (Q38 all languages, Q33 hinglish)
"How do I report ragging?" (Q38) and "college mein ambulance hai kya" (Q33 hinglish) hit the safety lexicon (harassment / medical), so the brain starts the emergency script and
transfer instead of answering the FAQ. Not changed: safety rules are never weakened to pass a question. Options for Harsh: (a) accept these as expected escalations, (b) reword the utterances
("anti-ragging committee kya hai", "campus mein hospital hai kya") for the demo script, (c) decide a policy that an informational ragging question may be answered with the entry AND offer a transfer (needs a brain change and your sign-off).
