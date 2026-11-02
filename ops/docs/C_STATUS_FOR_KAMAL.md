# Member C (ops) status for Kamal

| Finding | Status | Detail |
|---|---|---|
| **C-1** dashboard Publish fails in `RUNTIME_MODE=live` ("database is locked") | **Fixed**, commit `de47c2f` | Cookie authentication kept a `BEGIN IMMEDIATE` write lock open for the whole request, so the runtime's `GET /v1/kb/export` callback during publish could not get its own lock. Auth now uses a short session, read-only GETs use deferred transactions, and publish ends its session before calling the runtime. Test: `tests/test_kb_publish_live_callback.py`. |
| **C-2** `turns_count` one too high | **Fixed** (the commit that adds this file, "Ops: fix C-2 turn count independent of turn_index base") | Every turn count ops shows (call list, call detail, `call.turn` and `call.ended` stream events, live board) is now `COUNT(*)` of the stored turns for that call, never `turn_index + 1`. Works for 0-based, 1-based and gapped indexes; a duplicate turn does not change it. `CallEnd.turns_count` is stored as the runtime sent it, but ops shows the counted value and logs a warning when they differ. Ordering is still by `turn_index` ascending and idempotency on `(call_id, turn_index)` is unchanged. Test: `tests/test_turn_count.py`. |
| **C-3** dashboard "Answered by AI" stays 0 | **Not an ops bug** | A call only gets `outcome` / `ai_resolved` when the runtime ends it with `POST /v1/calls/{id}/end`. Typed-test calls are never ended (your finding A-2), so they stay in "Active calls". Once the runtime ends calls, the counts follow. |

Pull `origin/member-c-ops`. No change to any API path, status code or request body.
