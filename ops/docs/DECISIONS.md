# Member C Implementation Decisions

## Milestone 1: Foundation

### Decided (from prompt)
1. **Python 3.11**: Used via `py -V:Astral/CPython3.11` (3.11.15 available on system)
2. **Sync SQLAlchemy**: Using sync `def` endpoints with SQLAlchemy 2.x for simplicity
3. **SQLite WAL mode**: Enabled with busy_timeout=5000ms, foreign_keys=ON
4. **bcrypt directly**: Using bcrypt package directly instead of passlib (simpler, Windows-compatible)
5. **itsdangerous**: Using URLSafeTimedSerializer for session cookies (12h max age)
6. **Error codes**: Mapped 403 → "unauthorized" (contract doesn't define "forbidden")
7. **POST /v1/auth/me and /v1/auth/logout**: Added as additive extensions (needed by dashboard); noted in this document
8. **Four seed users**: admin@demo (admin, no dept), exam@demo/accounts@demo/hostel@demo (staff, with departments matching departments.yaml)
9. **Seed idempotency**: Check users table empty before seeding; fail fast if staff department invalid

### Additional decisions made

10. **Dependency injection simplification**: FastAPI routers created inline in app factory with settings closure, avoiding complex dependency overrides
11. **Login request body** (superseded by 24 and the length caps of 77): a local Pydantic LoginRequest model with email 254 / password 256 character caps
12. **Temp DB cleanup**: Windows file locking prevents immediate SQLite cleanup in pytest fixtures; tests pass but teardown shows PermissionError (known Windows issue, does not affect functionality)
13. **Config file encoding**: Using UTF-8 explicitly when loading YAML (contracts test has encoding issue on Windows)
14. **Session cookie settings**: httponly=True, samesite="lax", secure=False (dev), path="/"
15. **Audit on login**: Recording both login_success and login_failed events
16. **Error handler for HTTPException**: Custom handler maps status codes to ErrorEnvelope codes per contract
17. **Public healthz**: No authentication required for GET /healthz per OpenAPI spec
18. **CORS credentials**: allow_credentials=True to support session cookies from dashboard origin

### Deviations from prompt

None. All requirements from the prompt were implemented as specified.

### Known Limitations (Milestone 1; all resolved, kept for history)

1. Windows SQLite cleanup in fixtures: fixed in F3 (engine.dispose() in teardown).
2. The contracts tests pass 6/6 with `-X utf8`; the UTF-8 problem was an environment issue, not a contract problem.
3. Auth/config routes were moved to routers/ in F2.
4. `require_admin` is covered by the access matrix (tests/test_access_matrix.py).

### Next Steps for Milestone 2 (superseded: calls, tickets, turns and SSE are done; SMS, gaps, KB, simulator remain)

1. Implement calls, tickets, turns endpoints with idempotency
2. Add SMS service with policy enforcement
3. Implement gaps and KB entries/import/export/publish endpoints
4. Add SSE stream endpoint
5. Create simulator in dev/
6. Extract inline routes to proper router files as codebase grows

---

## Post-Milestone 1 Hardening (Fixes F1-F6)

### F1: UTF-8 on Windows
19. **UTF-8 mode**: All Python commands now use `-X utf8` flag; documented in pytest.ini and README.md
20. **Explicit encoding**: All file operations (open, read_text, write_text) use explicit `encoding="utf-8"` parameter
21. **Devanagari test**: Added test_utf8_support.py to verify non-ASCII content (Devanagari from urgency_rules.yaml) works correctly

### F2: Architecture Cleanup
22. **Routers extracted**: Moved all routes from app.py to routers/health.py, routers/auth_routes.py, routers/config_routes.py
23. **Standard dependency pattern**: Created deps.py with `get_settings`, `get_db`, `get_principal`, `require_admin`; app.state used for settings/engine/session_factory
24. **Login models**: Created local LoginRequest/LoginResponse models in resolvix_ops/models.py (ops-api.yaml does not define these in schemas)
25. **Error codes**: Confirmed ErrorEnvelope.code is Literal["validation_error", "authentication_required", "unauthorized", "not_found", "upstream_error", "permission_denied", "conflict", "unknown"]; using "unauthorized" for 403

### F3: Test Fixes
26. **Seeding tests**: Rewritten to copy seed_users.yaml to ops/api/ location instead of monkey-patching Settings.resolve_path (Pydantic prevents attribute assignment)
27. **Teardown**: Added engine.dispose() in conftest finalizer before temp file cleanup; using pytest tmp_path for DB files
28. **Error handlers**: Added StarletteHTTPException handler for 404 responses to return ErrorEnvelope
29. **All tests pass**: 22 passed, 0 failures, 0 errors; contracts tests: 6 passed

### F4: Smoke Test
30. **Real uvicorn smoke**: Created dev/smoke.py script that starts uvicorn, tests /healthz, /v1/departments, POST /v1/auth/login, GET /v1/auth/me, then terminates; all checks PASS

### F5: OpenAPI Coverage
31. **Coverage checker**: Created dev/check_openapi_coverage.py to compare ops-api.yaml (26 endpoints) to implemented routes (10 routes: 4 in spec + 6 extra)
32. **Coverage test**: Created tests/test_openapi_coverage.py asserting all implemented routes are either in ops-api.yaml or allowlist [/v1/auth/me, /v1/auth/logout, /docs, /openapi.json, /redoc, /docs/oauth2-redirect]
33. **Status at the end of Milestone 1**: 4/26 endpoints implemented; since then all 26 are implemented (see the Milestone 2 to 4 sections)



---

## Milestone 2: Calls, Tickets, SSE

Contract (ops-api.yaml + resolvix_contracts) is the source of truth. Every difference or ambiguity is listed here.

### Behaviour decisions
34. **Phone-number masking**: `mask_number` = first 3 chars + `•••••` + last 4; `•••` when shorter than 8; `None` stays `None`. Every list, detail, PATCH response, SSE payload and idempotent POST response is masked.
35. **`reveal=true`** (additive query param on `GET /v1/calls`, `GET /v1/calls/{id}`, `GET /v1/tickets`, `GET /v1/tickets/{id}`): returns full numbers and writes one `number_revealed` audit row per entity returned (actor = user email, or `service`). PATCH and the SSE stream never reveal. Any authenticated principal may reveal for now; role/department limits are deferred (see 41).
36. **`transcript_viewed` audit**: `GET /v1/calls/{id}` always writes it, in the same transaction as the optional `number_revealed` row.
37. **Idempotency, first write wins**: duplicate `POST /v1/calls` and `POST /v1/tickets` return 200 (`created:false`), duplicate turn returns 200 with the originally stored turn, a second `/end` changes nothing and returns 200 `{"status":"ok","updated":false}`. None of these emit an SSE event. A turn after `/end` is accepted (201, emits `call.turn`) and never produces a second `call.ended`.
38. **Ticket id allocation**: idempotency lookup and department check run before the sequence is read; the sequence update and the ticket insert are one transaction, so duplicates, 422s and failed commits never consume a number (tested, incl. a simulated commit failure). `<prefix>-<seq:04d>`, one global sequence starting at 1001. Ids above 9999 would break the contract pattern `^[A-Z]{2}-\d{4}$`; out of scope for the demo.
39. **BEGIN IMMEDIATE**: `isolation_level=None` on connect and `BEGIN IMMEDIATE` on the SQLAlchemy `begin` event, plus WAL and `busy_timeout=5000`. Every transaction (reads included) takes the write lock at start; transactions are short, so reads queue for milliseconds. A mutation check (plain `BEGIN`) makes the 20-thread create test fail with HTTP 500, so the test does guard the recipe. `expire_on_commit=False` avoids a second transaction just to refresh objects before publishing.
40. **SSE design**: one `SSEHub` per app (`app.state.sse_hub`); the serving loop is captured in the lifespan and cleared on shutdown; `publish()` uses `call_soon_threadsafe` so sync endpoints in the threadpool can call it, and never raises (the commit already succeeded). Bounded 100-event queue per subscriber, drop-oldest. Publish happens strictly after `commit()`; rollbacks, 422s and duplicates publish nothing. Keep-alive via sse-starlette `ping=15` plus a `: connected` comment on connect. Payloads are in `SSE_EVENTS.md`. `ticket.updated` is emitted on every PATCH (including a no-op one).
41. **Department scoping deferred** (P1 in the PRD): staff users currently see every department's tickets, calls and stream. Not implemented in M2 on purpose.
42. **Notifier seam**: `app.state.notifier` (`services/notify.py`, default no-op, injectable through `create_app(notifier=...)`). `on_ticket_status_changed(db, ticket, old, new, actor)` is called exactly once per real status change, after commit, never for same-status/note-only/assignee-only PATCHes. An exception in the notifier is logged and does not fail the PATCH. M3 replaces the implementation.
43. **PATCH rules**: any status transition is allowed (enum-validated, bad value 422, unknown ticket 404). A note is appended as `[<utc iso>] <actor>: <text>`. `status_changed` event only on a real change, `note_added` per note; every PATCH writes one `ticket_updated` audit row in the same transaction. `resolved_at` is stamped when status becomes `resolved`/`closed` and cleared when it returns to `open`/`in_progress`.
44. **Timestamps**: stored as naive UTC; inputs with an offset are converted; all output carries `+00:00` (contract: "ISO-8601 with offset").
45. **405 code**: ErrorBody.code has no `method_not_allowed`; 405 maps to `not_found` (closest). 400/422 map to `validation_error`; 403 maps to `unauthorized` (as in M1).
46. **Soft call reference**: `tickets.call_id` is no longer a foreign key, so a ticket for a call that was never registered (A restart, retry ordering) is accepted instead of failing with 500. Turns still require an existing call (404 otherwise).
47. **Lazy module-level `app`**: `resolvix_ops.app:app` is created on first access (PEP 562 `__getattr__`), so importing the module no longer opens the dev database. `uvicorn resolvix_ops.app:app` works unchanged. `dev/smoke.py` runs against a throwaway database file and deletes it.

### Contract differences and ambiguities
- **`rule_id` is not a ticket field.** `Ticket` and `TicketCreate` (extra=forbid) have no `rule_id`; it exists only on `TurnSummary`. The M2 draft parsed it out of `priority_reason`; that code, the ORM column and the field in API/SSE output were removed. The rule id stays inside `priority_reason` ("R-EXAM-60: ..."), which is how the contract documents it. If the dashboard wants a rule badge it can split that string, or B can add a field to the contract.
- **`GET /v1/calls` has no `active` filter in ops-api.yaml.** Added the additive `active=true` query param (calls with no `ended_at`) for the live board; documented here, not a new route so the coverage test needs no allowlist entry. Also added `limit` bounds (1..1000) and `reveal`.
- **`TicketPatch.resolution`** has no matching `Ticket` field. Stored as a note `Resolution: <text>` (plus a `note_added` event).
- **Response bodies are unspecified** for `POST /v1/calls` (`{"status":"ok","created":bool}`), `POST /v1/calls/{id}/end` (`{"status":"ok","updated":bool}`) and `POST /v1/tickets/{id}/events` (`{"status":"ok"}`); `GET /v1/calls` items (`CallSummary`) have no schema, so they use the `CallStart`+`CallEnd` field set.
- **`TicketEvent.payload` is `dict[str,str]`**: `status_changed` carries `{old_status,new_status}`, `note_added` carries `{note}`.
- **Masked numbers vs models**: the contract models accept masked strings (no phone pattern), so every response validates against `Ticket` / `TurnSummary` / `TicketEvent`; no `model_construct` was needed.
- **Turn path vs body**: if `TurnSummary.call_id` differs from the path call_id the request is rejected with 422 (contract does not say).
- **`gap.created`** is in the contract's event list but is emitted only from Milestone 4.
- Out-of-order `turns_count`: `max(existing, turn_index+1)` on turn append; `/end` sets the value A reports.


---

## Milestone 3: SMS service and policy

48. **Idempotency first**: the `idempotency_key` lookup runs before template/consent checks (same output, cheaper). Suppressed and failed attempts are stored too, so their duplicates return the stored record (HTTP 200, nothing re-sent). The duplicate returns the record's current state (e.g. `delivered` after a callback), keeping message_id and mode.
49. **Status codes for POST /v1/notifications/sms**: 202 for any new request (including one policy suppresses, since a SmsResult exists), 200 for a repeated key, 422 only for contract validation failures. A template id outside the contract's `SmsRequest.template_id` enum therefore gets 422 `validation_error`; `unknown_template` can only occur if `sms_templates.yaml` lacks an id the contract allows.
50. **Suppression reasons** (SmsResult.reason is a free string): `unknown_template, consent_not_allowed, invalid_e164, staff_number_not_allowed, missing_vars, vars_too_long, pii_in_vars, url_not_allowed`. Failed sends: `provider_timeout, provider_unreachable, provider_http_<code>, provider_bad_response`; callback failures: `provider_failed|provider_undelivered[_<twilio error code>]`. `vars_too_long` (200 per var, 480 rendered) is an addition to the prompt's list to keep SMS bounded.
51. **PII scan covers all supplied vars**, including extras that the template ignores, but only the vars the template uses are persisted (and none for suppressed requests).
52. **Mode mapping**: contract `SmsResult.mode` is `live|mock`. `NOTIFY_MODE` accepts `mock`, `twilio` (and `live` as alias); twilio maps to result mode `live`. `/healthz` reports `mock|live`, as in the yaml. `TWILIO_FROM` and the older `TWILIO_FROM_NUMBER` env names both work.
53. **Three short transactions** (insert `queued` -> provider call with no transaction open -> record outcome), so the SQLite write lock is never held during a network call. A crash between 1 and 3 leaves a `queued` record that is never retried (duplicates return it); acceptable for the demo.
54. **Retry policy**: one attempt plus exactly one retry on any provider failure or timeout (5 s each), no backoff; then `failed`, `flagged=true` (new `sms_messages.flagged` column) and an `sms_failed` ticket event. Mock provider never fails.
55. **Ticket events used**: contract `TicketEvent.kind` enum is `transfer_attempted, transfer_connected, transfer_failed, sms_queued, sms_sent, sms_failed, sms_suppressed, status_changed, note_added, callback_scheduled, brief_pushed`. Used: `sms_sent`, `sms_failed`, `sms_suppressed` (never `sms_queued`). Written only when the SMS has a `ticket_id` of an existing ticket. No new SSE events (PATCH still emits `ticket.updated`).
56. **Status callback `POST /v1/notifications/sms/status`** (additive, not in the yaml; allowlisted in the coverage test and the auth-matrix test): the only /v1 route besides login without service key/session. It validates `X-Twilio-Signature` (twilio `RequestValidator`, auth token) over `{OPS_PUBLIC_URL}/v1/notifications/sms/status` (the configured URL, not the request URL, so proxies cannot break it). Invalid, missing or mock-mode: 403 with `error.code = "unauthorized"` (contract has no "forbidden"). Unknown sid or intermediate status (queued/sending/sent): 200, no change. Final states (`delivered`, `failed`) are never overwritten.
57. **Status poll**: when `OPS_PUBLIC_URL` is localhost/127.0.0.1/::1, no StatusCallback is sent and one daemon `threading.Timer` (10 s) fetches the message status once. Gated by `SMS_POLL_ENABLED`; tests turn it off and use a fake Timer, and `SmsService.scheduled_polls` records what was scheduled.
58. **Real notifier** (`services/notify.py::SmsNotifier`, default in `create_app`): one `status_change` SMS per real status change, only for tickets with both `call_id` and `callback_number` (otherwise nothing, not even a suppressed record). Key `{ticket_id}:status:{new_status}`: flipping back to an earlier status does not send a second SMS for it. It runs synchronously after the PATCH commit in its own sessions (writes are committed before the response, so the next GET sees them); a Twilio outage can therefore delay a PATCH response by up to ~10 s (2 x 5 s). Exceptions are caught and logged by the route. Vars: `ticket_id` and `status` with underscores replaced by spaces ("in progress").
59. **No ticket_created SMS** (PRD queues SMS on status change only).
60. **Template content**: `contracts/config/sms_templates.yaml` unchanged. All four templates have the placeholders the code needs, no personal-data placeholders, `allowed_url_prefixes` already lists `https://college.example/`; only `answer_copy` can exceed one segment (bounded by the 480-char limit).
61. **Test isolation**: `test_settings` now uses `_env_file=None` and `notify_mode="mock"`, so the dev `.env` (Twilio credentials) can never leak into tests; Twilio tests mock HTTP with respx (`assert_all_mocked`), so any unrouted request raises instead of reaching the network. `dev/check_openapi_coverage.py` builds its app on a throwaway database.

### Contract differences and ambiguities (M3)
- `SmsResult.mode` is `live|mock` (prompt said "twilio"): `twilio` mode reports `live`.
- `SmsRequest.template_id` is a closed Literal, so unknown templates are 422 at validation, not a suppressed result.
- `SmsResult.reason` is an unconstrained string: reasons above are ours. `SmsResult.status` includes `queued` and `delivered`; `queued` is only visible transiently/after a crash, `delivered` after a Twilio callback or poll.
- `TicketEvent.payload` is `dict[str,str]`, so SMS event payload values are strings (`reason: ""` when none).
- ops-api.yaml lists only 202/200/422 for the SMS endpoint; no 401 documented (the global security block covers it).


---

## Overnight orchestration: gaps, KB, seed, simulator, login limiter

62. **Async status SMS**: in twilio mode (NOTIFY_ASYNC unset) the notifier submits to a 2-worker ThreadPoolExecutor on `app.state.sms_executor` (own sessions, failures only logged, drained at lifespan shutdown). Mock mode stays synchronous. Dedupe per (ticket, new status) is the SMS idempotency key, unchanged.
63. **Gap clustering** (agent G): keyword strategy behind `ClusterStrategy`; digit runs of 10+ in a question are masked before clustering/storage/SSE so a spoken phone number cannot leak into labels or events. `POST /v1/gaps` returns 201 `{status, gap_id, cluster_id, count}` (yaml gives no body); `GET /v1/gaps/clusters` has an additive `status` filter; PATCH requires `status` (422 otherwise). `gap.created` payload: gap_id, cluster_id, label, department_guess, count, ts.
64. **KB** (agent K): every save/import stores a draft (the yaml says import keeps status; the orchestrator rule wins, so a stale approved entry can never re-enter the export unreviewed); a flagged entry cannot be approved (409); `GET /v1/kb/entries` adds sibling `flagged_ids` and `meta` because `KBEntry` forbids extra fields; admin session only for status changes and publish (403 `unauthorized` for staff and the service key); mock publish returns the fixture with real counts and a "mock publish" warning; live publish maps A's 422 to 422 and everything else non-200 to 502. `GET /v1/kb/publishes` is additive (allowlisted).
65. **Login rate limit**: 10 attempts per minute per client IP, in memory (`SlidingWindowLimiter`), 429 `rate_limited` with `Retry-After`; `LOGIN_RATE_LIMIT_PER_MIN=0` disables it. It counts successful logins too. Behind a reverse proxy every user would share one IP; acceptable for the demo.
66. **Seed and simulator** (agent S): the simulator skips every SMS step when `/healthz` reports `notify_mode: live`, so fixture numbers can never be texted; outcomes are derived from the stream; reruns use fresh call ids and idempotency keys.
67. **Test fixtures**: `tests/helpers/fixtures.py` gives ops_app / service_client / admin_client / staff_client / anon_client / freeze_clock / app_factory.
68. **Department scoping (PRD P1-07, done)**: `scope.py`. Staff (role staff) see only their department: ticket list/detail/PATCH/events, gap clusters (cluster.department == theirs; clusters with no department are admin-only), and calls that produced at least one ticket of their department. Anything outside scope answers 404 (existence not revealed); staff without a department see nothing. Admin and the service key see everything. The SSE stream is filtered per subscriber: staff get ticket.* and alert.p1 of their department and gap.created whose department_guess matches; `call.*` events are not sent to staff because a call has no department until a ticket exists (so the staff live board shows tickets but not live calls; reversible with STAFF_SCOPE_ENABLED=false). KB entries are not department-scoped (KB publish stays admin-only).
69. **`last_turn` on GET /v1/calls items** (additive): latest stored turn {turn_index, path, tier, lang} or null, so the live board survives a reload.

## Review fixes (REVIEW.md, applied after the independent review)
70. **Access matrix** (tests/test_access_matrix.py is the source of truth): staff sessions may read everything in scope, PATCH tickets and gap clusters, save KB drafts (never over approved/archived entries) and reveal numbers on detail endpoints; the service key (and admin) may call the system write endpoints (calls, turns, end, tickets, ticket events, SMS, gaps); KB approve/archive/import/publish are admin only; the service key never writes the KB. Staff get 403 on the system endpoints (REVIEW H1).
71. **Sessions** (H2): the cookie carries `sv` = users.session_version; every request re-reads the user (role and department come from the database); logout bumps the version (all sessions of that user end); the cookie has Max-Age 12 h and Secure when OPS_PUBLIC_URL is https; an open /v1/stream re-checks its session every ping interval and closes when it is revoked.
72. **KB injection scan** (H3): text is normalised (NFKC, zero-width removed, homoglyphs mapped, punctuation collapsed) before matching; patterns widened (ignore/forget/override ... instructions/rules/prompt, system prompt, you are now, jailbreak, Hinglish and Devanagari variants). Still a hint for the approving admin, not a security boundary.
73. **SMS policy** (M1, M2): any dotted host, dotted quad or "://" is a URL and must sit under an allowed prefix (ASCII, no ".."); PII rules are person-linked: marks/grades/scores, personal dues words next to a number (years excluded), money with a person marker, roll number with a name. Generic fee answers pass. Reasons: marks_or_grade, personal_dues, money_with_person, roll_number_with_name.
74. **Reveal** (M3) only on GET /v1/tickets/{id} and GET /v1/calls/{id}; lists answer 422. Event payload numbers, access-log numbers and (for staff) department numbers are masked (M8); the 500 handler logs type and frames, never the message (L6).
75. **KB writes** (M5, M6): import is admin only, keeps each line status except flagged lines (draft), reports a repeated id in one file; the contract sentence "entries keep their status field" is honoured again.
76. **Status SMS ordering** (M7): ShardedExecutor (2 single-thread workers keyed by ticket id) keeps FIFO per ticket; a background send publishes ticket.updated when its outcome is stored. `SmsMessage.flagged` is still only visible through the sms_failed ticket event (no list endpoint: not required by the contract).
77. **Login** (M9): plain def (threadpool), a bcrypt comparison also for unknown emails, limiter on failed attempts per (ip, email) plus 6x that per ip for all attempts, 429 rate_limited with Retry-After.
78. **Gap POST idempotency**: not implemented. GapCreate has no idempotency key in the contract, so a retry by A counts twice; documented, not required.
79. **Docs routes** (/docs, /redoc, /openapi.json) exist only when OPS_PUBLIC_URL is localhost. Ticket ids stop at 9999 with a 500 rather than breaking the contract pattern.

80. **Forbidden manifest regenerated after PR #1 (Member B), by agreement.** brain/ and the extended contracts/config/urgency_rules.yaml are now the legitimate baseline. guard_forbidden.py also ignores *.egg-info, __pycache__, .venv and node_modules at any depth, skips forbidden paths that are absent in a fresh clone, uses forward-slash keys and hashes with CRLF folded to LF so Windows and Linux checkouts agree.
81. **Ticket type label**: the dashboard shows ticket types as plain readable text (SYSTEM_FAILURE -> "System failure"); the Rules page renders unknown rule actions, ticket types and extra top-level keys of urgency_rules.yaml (e.g. human_request_exclusions) generically.
82. **Forbidden manifest regenerated after PR #2 (Member B), by agreement.** contracts/config/urgency_rules.yaml (safety_exclusions, extended human_request_exclusions) and brain/ changed legitimately; the Rules page shows safety_exclusions as its own collapsible section.
83. **Manifest regenerated again after PR #3 (Member A voice-runtime)** was merged into member-c-ops while PR #2 was being verified; voice-runtime/ is now part of the legitimate baseline.
84. **Manifest regenerated after PR #9 (Member A audit fixes), PR #10 / #8 (Member B draft AKGEC KB, eval sets) merged into integration**, by agreement. New files under brain/data and brain/eval are legitimate baseline.
85. **Voice-path latency** (dev/latency_check.py, 200 sequential requests each, real uvicorn, file DB, mock SMS, one keep-alive connection): POST /v1/calls/{id}/turns p50 9.7 ms p95 12.7; POST /v1/tickets p50 11.2 p95 15.2; POST /v1/notifications/sms (mock) p50 17.4 p95 19.4; POST /v1/gaps p50 14.7 p95 17.4 (max 68.9 once); POST ticket event p50 11.3 p95 12.8. All p95 are under 20 ms (target 50 ms), so no change was needed; the script exits 1 when any p95 exceeds the limit.
86. **Demo-day soak** (dev/soak_check.py): 70 cycles x 9 scenarios (630 calls, 420 tickets, speed 0) against uvicorn + file DB with an SSE client connected: server threads stayed at 6 and handles at 224 from the first to the last sample, the WAL stayed at about 4 MB (SQLite autocheckpoint), the database grew about 2 KB per call (1.2 MB after 630 calls), and the SSE client received every event (2520). SSE reconnect was checked in a real browser: killing the API showed "Reconnecting in 2s (try 2)", and 20 s after the restart the status was "Live" again and a P1 ticket created through the API appeared with its alert banner, without a page refresh.
