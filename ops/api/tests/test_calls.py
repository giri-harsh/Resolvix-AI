"""Calls: register, turns, end, detail, list, masking, audit."""
from datetime import datetime, timedelta, timezone

from _m2 import audit_rows, drain, settle
from resolvix_ops.seed_users import seed_users_if_needed


def start(client, auth, fixture_json, **kw):
    body = fixture_json("call_start")
    body.update(kw)
    return client.post("/v1/calls", json=body, headers=auth), body


def turn_body(fixture_json, call_id="call-urgent-exam", index=0, **kw):
    t = fixture_json("turn_summary")
    t["call_id"] = call_id
    t["turn_index"] = index
    t.update(kw)
    return t


def test_start_then_duplicate(client, auth, fixture_json):
    r, body = start(client, auth, fixture_json)
    assert r.status_code == 201 and r.json()["created"] is True
    r2 = client.post("/v1/calls", json=body, headers=auth)
    assert r2.status_code == 200 and r2.json()["created"] is False
    assert len(client.get("/v1/calls", headers=auth).json()["items"]) == 1


def test_start_validation_error_envelope(client, auth):
    r = client.post("/v1/calls", json={"call_id": "x"}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_turn_created_duplicate_first_write_wins(client, auth, fixture_json):
    start(client, auth, fixture_json)
    t = turn_body(fixture_json, index=0, assistant_text="first")
    r1 = client.post("/v1/calls/call-urgent-exam/turns", json=t, headers=auth)
    assert r1.status_code == 201
    t2 = turn_body(fixture_json, index=0, assistant_text="SECOND")
    r2 = client.post("/v1/calls/call-urgent-exam/turns", json=t2, headers=auth)
    assert r2.status_code == 200
    assert r2.json()["assistant_text"] == "first"
    detail = client.get("/v1/calls/call-urgent-exam", headers=auth).json()
    assert [x["assistant_text"] for x in detail["turns"]] == ["first"]
    assert detail["call"]["turns_count"] == 1


def test_turns_returned_in_index_order(client, auth, fixture_json):
    start(client, auth, fixture_json)
    for i in (2, 0, 1):
        r = client.post("/v1/calls/call-urgent-exam/turns", json=turn_body(fixture_json, index=i), headers=auth)
        assert r.status_code == 201
    detail = client.get("/v1/calls/call-urgent-exam", headers=auth).json()
    assert [x["turn_index"] for x in detail["turns"]] == [0, 1, 2]
    assert detail["call"]["turns_count"] == 3


def test_turn_path_body_call_id_mismatch_is_422(client, auth, fixture_json):
    start(client, auth, fixture_json)
    r = client.post("/v1/calls/call-urgent-exam/turns", json=turn_body(fixture_json, call_id="other"), headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_end_then_second_end_returns_original(client, auth, fixture_json):
    start(client, auth, fixture_json)
    end = fixture_json("call_end")
    r1 = client.post("/v1/calls/call-urgent-exam/end", json=end, headers=auth)
    assert r1.status_code == 200
    end2 = dict(end, outcome="failed", ai_resolved=True, turns_count=9)
    r2 = client.post("/v1/calls/call-urgent-exam/end", json=end2, headers=auth)
    assert r2.status_code == 200
    call = client.get("/v1/calls/call-urgent-exam", headers=auth).json()["call"]
    assert call["outcome"] == "transferred" and call["ai_resolved"] is False and call["turns_count"] == 0  # counted: no turn was stored (the runtime said 3)


def test_turn_after_end_is_accepted(client, auth, fixture_json):
    start(client, auth, fixture_json)
    client.post("/v1/calls/call-urgent-exam/end", json=fixture_json("call_end"), headers=auth)
    r = client.post("/v1/calls/call-urgent-exam/turns", json=turn_body(fixture_json, index=5), headers=auth)
    assert r.status_code == 201
    call = client.get("/v1/calls/call-urgent-exam", headers=auth).json()["call"]
    assert call["ended_at"] is not None and call["turns_count"] == 1  # counted, not index + 1


def test_404s(client, auth, fixture_json):
    for r in (
        client.post("/v1/calls/nope/turns", json=turn_body(fixture_json, call_id="nope"), headers=auth),
        client.post("/v1/calls/nope/end", json=fixture_json("call_end"), headers=auth),
        client.get("/v1/calls/nope", headers=auth),
    ):
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_masking_in_list_and_detail_and_start_response(client, auth, fixture_json):
    r, _ = start(client, auth, fixture_json)
    assert "9000000002" not in r.text
    lst = client.get("/v1/calls", headers=auth).json()["items"][0]
    assert lst["caller_number"] == "+91•••••0002"
    det = client.get("/v1/calls/call-urgent-exam", headers=auth).json()["call"]
    assert det["caller_number"] == "+91•••••0002"


def test_reveal_returns_full_number_and_audits(client, auth, fixture_json):
    start(client, auth, fixture_json)
    det = client.get("/v1/calls/call-urgent-exam?reveal=true", headers=auth).json()["call"]
    assert det["caller_number"] == "+919000000002"
    rows = audit_rows(client, "number_revealed")
    assert rows == [("service", "number_revealed", "call", "call-urgent-exam", {"field": "caller_number"})]
    r = client.get("/v1/calls?reveal=true", headers=auth)  # no bulk reveal on lists
    assert r.status_code == 422 and "9000000002" not in r.text
    client.get("/v1/calls", headers=auth)  # no reveal -> nothing more audited
    assert len(audit_rows(client, "number_revealed")) == 1


def test_transcript_viewed_audit_service_and_user(client, auth, fixture_json, test_settings):
    start(client, auth, fixture_json)
    client.get("/v1/calls/call-urgent-exam", headers=auth)
    assert audit_rows(client, "transcript_viewed")[-1][:4] == (
        "service", "transcript_viewed", "call", "call-urgent-exam")

    with client.app.state.session_factory() as db:
        keys = {d.key for d in client.app.state.departments_config.departments}
        seed_users_if_needed(db, test_settings, keys)
    r = client.post("/v1/auth/login", json={"email": "admin@demo", "password": test_settings.seed_password})
    assert r.status_code == 200
    client.get("/v1/calls/call-urgent-exam")  # the cookie jar carries the session; actor is the email
    assert audit_rows(client, "transcript_viewed")[-1][0] == "admin@demo"


def test_list_filters(client, auth, fixture_json):
    base = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
    for i, outcome in enumerate(["answered", "escalated", None]):
        start(client, auth, fixture_json, call_id=f"c{i}", started_at=(base + timedelta(hours=i)).isoformat())
        if outcome:
            end = fixture_json("call_end")
            end.update(outcome=outcome, ended_at=(base + timedelta(hours=i, minutes=1)).isoformat())
            client.post(f"/v1/calls/c{i}/end", json=end, headers=auth)

    def ids(q):
        return [c["call_id"] for c in client.get(f"/v1/calls{q}", headers=auth).json()["items"]]

    assert ids("") == ["c2", "c1", "c0"]  # newest first
    assert ids("?outcome=escalated") == ["c1"]
    assert ids("?active=true") == ["c2"]
    assert ids("?limit=2") == ["c2", "c1"]
    since = (base + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert ids("?from=" + since) == ["c2", "c1"]


def test_timestamps_have_offset_and_are_utc(client, auth, fixture_json):
    start(client, auth, fixture_json)  # fixture is +05:30
    call = client.get("/v1/calls/call-urgent-exam", headers=auth).json()["call"]
    assert call["started_at"] == "2026-10-23T04:45:00+00:00"


def test_sse_events_for_calls_and_no_events_on_duplicates(client, auth, fixture_json):
    hub = client.app.state.sse_hub
    _, q = hub.subscribe()
    _, body = start(client, auth, fixture_json)
    client.post("/v1/calls", json=body, headers=auth)  # duplicate
    t = turn_body(fixture_json, index=0)
    client.post("/v1/calls/call-urgent-exam/turns", json=t, headers=auth)
    client.post("/v1/calls/call-urgent-exam/turns", json=t, headers=auth)  # duplicate
    client.post("/v1/calls/call-urgent-exam/end", json=fixture_json("call_end"), headers=auth)
    client.post("/v1/calls/call-urgent-exam/end", json=fixture_json("call_end"), headers=auth)  # second end
    client.post("/v1/calls/call-urgent-exam/turns", json=turn_body(fixture_json, index=1), headers=auth)  # after end
    events = drain(q, 4) + settle(q)
    assert [n for n, _ in events] == ["call.started", "call.turn", "call.ended", "call.turn"]
    for _, data in events:
        assert "ts" in data
    assert events[0][1]["caller_number"] == "+91•••••0002"
