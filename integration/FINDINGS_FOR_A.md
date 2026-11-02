# Findings for Member A (voice runtime). Nothing in `voice-runtime/**` was edited.

## A-1 (resolved in PR #9, verified): `BRAIN_IMPL=real` no longer falls back silently
`voice-runtime/resolvix_runtime/brain_loader.py:32-37` now raises `RuntimeError("BRAIN_IMPL is set to 'real', but resolvix_brain cannot be imported ...")`.
Verified by forcing the import to fail (`sys.modules['resolvix_brain'] = None`): the runtime raised instead of using MockBrain. `integration/run_all.ps1`
additionally checks that `/healthz` -> `brain.embedder` exists (only the real Brain reports it).

## A-2: the typed test endpoint never ends the call
`resolvix_runtime/app.py:116-` (`POST /api/test/turn`) starts the call and posts turns but never calls `ops_client.post_call_end` or `brain.end_call` (the production shim does: `shim.py:183-190`). Effects: every typed-test call stays "active" on the dashboard live board forever, "Answered by AI" stays 0, and Brain sessions are only freed by the 2 h TTL. Suggested: a `POST /api/test/end` (or end after N seconds idle).

## A-3: no caller number on the test channel
`app.py:124-129` creates `CallContext(caller_number=None)`, so on the typed console the Brain cannot send "ticket created" SMS to the caller (it asks for a number instead). Suggested: an optional `caller_number` field in `TestTurnRequest` so the SMS and callback demos can be shown without a phone.

## A-4 (observation): ops down makes a turn that needs a ticket wait ~10 s
With ops stopped, two typed turns (an answer, then a human request) took 20.6 s in total; the caller still got an answer and a "registered, staff will call" line, and the ticket was delivered after ops came back (queued ticket arrived as HS-1002). Consider a shorter ops timeout on the hot path so the caller is not silent for ~10 s.

## A-5 (observation): simulated transfers are recorded as `transfer_attempted` then `transfer_failed (reason: simulated)`
On the test channel every transfer shows as failed in the ticket timeline (`mode: simulated`). Harmless, but staff will see "transfer failed" on every typed test.

## Verified OK
Real Brain loads (`/healthz` brain.embedder = intfloat/multilingual-e5-small, KB fetched from ops with the key: `GET /v1/kb/export 200`, 12 entries); ticket-first (create_ticket before transfer, and before the staff_alert SMS); `{{ticket_id}}` spoken digit by digit; `/admin/kb/reload` rejects a wrong key (401) and returned KBVersionInfo with the right key; duplicate deliveries are idempotent; ops outage drill: answers continue, tickets queued and flushed on restart. Own suites: voice-runtime 16 passed; `integration/e2e_test.py` (Member A's) 9 passed against the running real-Brain stack.
