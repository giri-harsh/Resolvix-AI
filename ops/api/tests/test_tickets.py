"""Tickets: create/idempotency/sequence, concurrency, sort + filters, PATCH, events, masking."""
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import event

from _m2 import audit_rows, make_ticket_body
from resolvix_contracts import Ticket as TicketModel


def create(client, auth, fixture_json, key, **kw):
    return client.post("/v1/tickets", json=make_ticket_body(fixture_json, key, **kw), headers=auth)


# ------------------------------------------------------------------ create / idempotency / sequence
def test_create_201_then_duplicate_200_same_id_no_sequence_consumed(client, auth, fixture_json):
    r1 = create(client, auth, fixture_json, "k1")
    assert r1.status_code == 201
    assert r1.json() == {"ticket_id": "HD-1001", "created": True}
    r2 = create(client, auth, fixture_json, "k1")
    assert r2.status_code == 200
    assert r2.json() == {"ticket_id": "HD-1001", "created": False}
    r3 = create(client, auth, fixture_json, "k2")
    assert r3.json()["ticket_id"] == "HD-1002"  # the duplicate did not burn 1002


def test_prefixes_and_global_sequence_across_departments(client, auth, fixture_json):
    expected = [("exam_cell", "EX"), ("accounts", "AC"), ("hostel", "HS"), ("helpdesk", "HD"), ("exam_cell", "EX")]
    for i, (dept, prefix) in enumerate(expected):
        r = create(client, auth, fixture_json, f"k{i}", department=dept)
        assert r.json()["ticket_id"] == f"{prefix}-{1001 + i}"


def test_unknown_department_422_envelope_and_no_number_consumed(client, auth, fixture_json):
    r = create(client, auth, fixture_json, "bad", department="no_such_dept")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert create(client, auth, fixture_json, "good").json()["ticket_id"] == "HD-1001"


def test_invalid_body_422(client, auth):
    r = client.post("/v1/tickets", json={"idempotency_key": "x"}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_ticket_for_unregistered_call_is_accepted(client, auth, fixture_json):
    r = create(client, auth, fixture_json, "k", call_id="never-registered")
    assert r.status_code == 201


def test_rule_id_is_not_part_of_the_ticket_contract(client, auth, fixture_json):
    create(client, auth, fixture_json, "k", priority_reason="R-EXAM-60: exam in 15 min")
    body = client.get("/v1/tickets/HD-1001", headers=auth).json()
    assert "rule_id" not in body
    assert body["priority_reason"] == "R-EXAM-60: exam in 15 min"
    TicketModel.model_validate(body)  # extra="forbid" -> would raise on a stray field


def test_failed_commit_does_not_consume_a_number(test_settings, auth, fixture_json, fail_next_commit):
    from fastapi.testclient import TestClient
    from resolvix_ops.app import create_app
    app = create_app(test_settings)
    with TestClient(app, raise_server_exceptions=False) as c:
        fail_next_commit()
        r = create(c, auth, fixture_json, "k-fail")
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
        ok = create(c, auth, fixture_json, "k-ok")
        assert ok.json()["ticket_id"] == "HD-1001"
        # the failed key was not stored either: retrying it creates a ticket
        assert create(c, auth, fixture_json, "k-fail").json() == {"ticket_id": "HD-1002", "created": True}
    app.state.engine.dispose()


def test_20_concurrent_creates_gapless_distinct_ids(live_server, fixture_json):
    headers = {"X-Service-Key": "test-service-key-12345"}
    depts = ["exam_cell", "accounts", "hostel", "helpdesk"]
    barrier = threading.Barrier(20)

    def worker(i):
        with httpx.Client(base_url=live_server.base_url, timeout=30) as c:
            barrier.wait(timeout=10)
            r = c.post("/v1/tickets", json=make_ticket_body(fixture_json, f"conc-{i}", department=depts[i % 4]),
                       headers=headers)
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(worker, range(20)))
    assert [s for s, _ in results] == [201] * 20, results
    ids = [b["ticket_id"] for _, b in results]
    assert len(set(ids)) == 20
    assert sorted(int(i.split("-")[1]) for i in ids) == list(range(1001, 1021))  # gapless


def test_concurrent_duplicates_of_one_key_create_one_ticket(live_server, fixture_json):
    headers = {"X-Service-Key": "test-service-key-12345"}
    barrier = threading.Barrier(10)

    def worker(_):
        with httpx.Client(base_url=live_server.base_url, timeout=30) as c:
            barrier.wait(timeout=10)
            r = c.post("/v1/tickets", json=make_ticket_body(fixture_json, "same-key"), headers=headers)
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker, range(10)))
    assert sorted(s for s, _ in results) == [200] * 9 + [201]
    assert {b["ticket_id"] for _, b in results} == {"HD-1001"}


def test_begin_immediate_is_in_effect_and_reads_still_work(client, auth, fixture_json):
    seen = []
    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: seen.append(stmt.strip().upper()))
    create(client, auth, fixture_json, "k")
    writes = list(seen)
    seen.clear()
    assert client.get("/v1/tickets", headers=auth).status_code == 200
    assert any(s.startswith("BEGIN IMMEDIATE") for s in writes)          # the create takes the write lock at once
    assert "BEGIN" in seen and not any(s.startswith("BEGIN IMMEDIATE") for s in seen)  # the list read does not (C-1)
    # the connection really is in manual-transaction mode and WAL
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


# ------------------------------------------------------------------ list: sort + filters
def seed_mixed(client, auth, fixture_json):
    spec = [
        ("a", "P3", "helpdesk", "+919000000001"),
        ("b", "P1", "exam_cell", "+919000000002"),
        ("c", "P2", "accounts", "+919000000001"),
        ("d", "P1", "hostel", "+919000000003"),
        ("e", "P3", "exam_cell", "+919000000002"),
    ]
    ids = {}
    for key, prio, dept, num in spec:
        r = create(client, auth, fixture_json, key, priority=prio, department=dept,
                   caller_number=num, callback_number=num)
        ids[key] = r.json()["ticket_id"]
    return ids


def test_sort_priority_then_newest_first(client, auth, fixture_json):
    ids = seed_mixed(client, auth, fixture_json)
    items = client.get("/v1/tickets", headers=auth).json()["items"]
    assert [t["ticket_id"] for t in items] == [ids[k] for k in ("d", "b", "c", "e", "a")]


def test_filters_alone_and_combined(client, auth, fixture_json):
    ids = seed_mixed(client, auth, fixture_json)
    client.patch(f"/v1/tickets/{ids['b']}", json={"status": "in_progress"}, headers=auth)

    def got(q):
        return [t["ticket_id"] for t in client.get(f"/v1/tickets{q}", headers=auth).json()["items"]]

    assert got("?status=in_progress") == [ids["b"]]
    assert got("?status=open") == [ids[k] for k in ("d", "c", "e", "a")]
    assert got("?department=exam_cell") == [ids["b"], ids["e"]]
    assert got("?priority=P3") == [ids["e"], ids["a"]]
    assert got("?caller_number=%2B919000000001") == [ids["c"], ids["a"]]
    assert got("?department=exam_cell&priority=P3") == [ids["e"]]
    assert got("?caller_number=%2B919000000002&status=open") == [ids["e"]]
    assert got("?priority=P1&department=hostel&status=open") == [ids["d"]]
    assert got("?department=transport") == []
    assert got("?limit=2") == [ids["d"], ids["b"]]


# ------------------------------------------------------------------ PATCH
def test_patch_status_change_notifier_once_event_audit(client_n, notifier, auth, fixture_json):
    client = client_n
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    r = client.patch(f"/v1/tickets/{tid}", json={"status": "in_progress"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    assert notifier.calls == [(tid, "open", "in_progress", "service")]
    events = client.get(f"/v1/tickets/{tid}/events", headers=auth).json()["items"]
    assert [e["kind"] for e in events] == ["status_changed"]
    assert events[0]["payload"] == {"old_status": "open", "new_status": "in_progress"}
    assert audit_rows(client, "ticket_updated")[0][:4] == ("service", "ticket_updated", "ticket", tid)


def test_patch_same_status_or_note_only_does_not_notify(client_n, notifier, auth, fixture_json):
    client = client_n
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    client.patch(f"/v1/tickets/{tid}", json={"status": "open"}, headers=auth)           # not a change
    client.patch(f"/v1/tickets/{tid}", json={"note": "called back"}, headers=auth)      # note only
    client.patch(f"/v1/tickets/{tid}", json={"assignee": "exam@demo"}, headers=auth)    # assignee only
    assert notifier.calls == []
    kinds = [e["kind"] for e in client.get(f"/v1/tickets/{tid}/events", headers=auth).json()["items"]]
    assert kinds == ["note_added"]  # no status_changed
    assert len(audit_rows(client, "ticket_updated")) == 3  # every PATCH is audited


def test_patch_notifier_called_once_per_real_change(client_n, notifier, auth, fixture_json):
    client = client_n
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    for s in ("in_progress", "in_progress", "resolved", "open", "closed"):
        client.patch(f"/v1/tickets/{tid}", json={"status": s}, headers=auth)
    assert [(o, n) for _, o, n, _ in notifier.calls] == [
        ("open", "in_progress"), ("in_progress", "resolved"), ("resolved", "open"), ("open", "closed")]


def test_patch_notes_are_appended_with_timestamp_and_actor(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    client.patch(f"/v1/tickets/{tid}", json={"note": "first"}, headers=auth)
    body = client.patch(f"/v1/tickets/{tid}", json={"note": "second"}, headers=auth).json()
    assert len(body["notes"]) == 2
    assert body["notes"][0].endswith("] service: first") and body["notes"][0].startswith("[20")
    assert body["notes"][1].endswith("] service: second")
    assert client.get(f"/v1/tickets/{tid}", headers=auth).json()["notes"] == body["notes"]


def test_patch_resolved_at_follows_status(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    assert client.patch(f"/v1/tickets/{tid}", json={"status": "resolved"}, headers=auth).json()["resolved_at"]
    assert client.patch(f"/v1/tickets/{tid}", json={"status": "open"}, headers=auth).json()["resolved_at"] is None


def test_patch_contract_fixture(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    r = client.patch(f"/v1/tickets/{tid}", json=fixture_json("ticket_patch"), headers=auth)
    assert r.status_code == 200
    body = r.json()
    TicketModel.model_validate(body)
    assert body["status"] == "resolved"
    assert any("Room 3.12 confirmed by phone" in n for n in body["notes"])
    assert any("Resolution: Student directed to room 3.12" in n for n in body["notes"])


def test_patch_invalid_status_422_and_unknown_ticket_404(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    r = client.patch(f"/v1/tickets/{tid}", json={"status": "bogus"}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.patch("/v1/tickets/XX-9999", json={"status": "open"}, headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_patch_survives_notifier_failure(test_settings, auth, fixture_json):
    from fastapi.testclient import TestClient
    from resolvix_ops.app import create_app
    from resolvix_ops.services.notify import Notifier

    class Boom(Notifier):
        def on_ticket_status_changed(self, *a, **k):
            raise RuntimeError("sms provider down")

    app = create_app(test_settings, notifier=Boom())
    with TestClient(app) as c:
        tid = create(c, auth, fixture_json, "k").json()["ticket_id"]
        r = c.patch(f"/v1/tickets/{tid}", json={"status": "resolved"}, headers=auth)
        assert r.status_code == 200 and r.json()["status"] == "resolved"
    app.state.engine.dispose()


# ------------------------------------------------------------------ events endpoints
def test_events_post_and_get_oldest_first(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    ev = fixture_json("ticket_event")
    assert client.post(f"/v1/tickets/{tid}/events", json=ev, headers=auth).status_code == 201
    earlier = dict(ev, kind="transfer_attempted", at="2026-10-23T10:14:00+05:30", payload={"to_number": "x"})
    client.post(f"/v1/tickets/{tid}/events", json=earlier, headers=auth)
    items = client.get(f"/v1/tickets/{tid}/events", headers=auth).json()["items"]
    assert [e["kind"] for e in items] == ["transfer_attempted", "transfer_failed"]
    assert items[1]["at"] == "2026-10-23T04:45:00+00:00" and items[1]["actor"] == "system"


def test_events_invalid_kind_422_and_unknown_ticket_404(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k").json()["ticket_id"]
    bad = dict(fixture_json("ticket_event"), kind="not_an_event")
    assert client.post(f"/v1/tickets/{tid}/events", json=bad, headers=auth).status_code == 422
    assert client.post("/v1/tickets/XX-1/events", json=fixture_json("ticket_event"), headers=auth).status_code == 404
    assert client.get("/v1/tickets/XX-1/events", headers=auth).status_code == 404


# ------------------------------------------------------------------ masking
def test_masking_list_detail_create_response_and_reveal(client, auth, fixture_json):
    r = create(client, auth, fixture_json, "k")
    assert "9000000006" not in r.text  # idempotent POST responses never echo numbers
    assert "9000000006" not in create(client, auth, fixture_json, "k").text
    tid = r.json()["ticket_id"]

    item = client.get("/v1/tickets", headers=auth).json()["items"][0]
    assert item["caller_number"] == "+91•••••0006" and item["callback_number"] == "+91•••••0006"
    det = client.get(f"/v1/tickets/{tid}", headers=auth).json()
    assert det["caller_number"] == "+91•••••0006"
    assert audit_rows(client, "number_revealed") == []

    full = client.get(f"/v1/tickets/{tid}?reveal=true", headers=auth).json()
    assert full["caller_number"] == "+919000000006" and full["callback_number"] == "+919000000006"
    r = client.get("/v1/tickets?reveal=true", headers=auth)  # no bulk reveal on lists
    assert r.status_code == 422 and "9000000006" not in r.text
    rows = audit_rows(client, "number_revealed")
    assert len(rows) == 1 and rows[0][:4] == ("service", "number_revealed", "ticket", tid)

    # PATCH never reveals
    assert client.patch(f"/v1/tickets/{tid}", json={"note": "n"}, headers=auth).json()["caller_number"] == "+91•••••0006"


def test_null_numbers_stay_null(client, auth, fixture_json):
    tid = create(client, auth, fixture_json, "k", caller_number=None, callback_number=None,
                 callback_requested=False).json()["ticket_id"]
    det = client.get(f"/v1/tickets/{tid}?reveal=true", headers=auth).json()
    assert det["caller_number"] is None and det["callback_number"] is None
    assert audit_rows(client, "number_revealed") == []
