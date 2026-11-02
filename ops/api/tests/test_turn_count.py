"""C-2: every turn count ops shows is COUNT(*) of stored turns, independent of the turn_index base."""
import logging

from _m2 import drain, settle
from test_calls import start, turn_body

CALL = "call-urgent-exam"


def post_turns(client, auth, fixture_json, indexes):
    for i in indexes:
        r = client.post(f"/v1/calls/{CALL}/turns", json=turn_body(fixture_json, index=i), headers=auth)
        assert r.status_code == 201, (i, r.text)


def count_of(client, auth):
    detail = client.get(f"/v1/calls/{CALL}", headers=auth).json()
    listed = [c for c in client.get("/v1/calls", headers=auth).json()["items"] if c["call_id"] == CALL][0]
    assert detail["call"]["turns_count"] == listed["turns_count"] == len(detail["turns"])  # detail, list and rows agree
    return detail["call"]["turns_count"]


def test_zero_based_indexes(client, auth, fixture_json):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [0, 1, 2])
    assert count_of(client, auth) == 3


def test_one_based_indexes(client, auth, fixture_json):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [1, 2])
    assert count_of(client, auth) == 2  # used to report 3


def test_gap_in_indexes_counts_stored_turns(client, auth, fixture_json):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [1, 2, 4])
    assert count_of(client, auth) == 3


def test_duplicate_turn_does_not_change_the_count(client, auth, fixture_json):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [1, 2])
    again = client.post(f"/v1/calls/{CALL}/turns", json=turn_body(fixture_json, index=2), headers=auth)
    assert again.status_code == 200
    assert count_of(client, auth) == 2


def test_out_of_order_arrival_and_ordering_by_index(client, auth, fixture_json):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [3, 1, 2])
    detail = client.get(f"/v1/calls/{CALL}", headers=auth).json()
    assert [t["turn_index"] for t in detail["turns"]] == [1, 2, 3] and count_of(client, auth) == 3


def test_no_turns_is_zero(client, auth, fixture_json):
    start(client, auth, fixture_json)
    assert count_of(client, auth) == 0


def test_stream_events_carry_the_counted_value(client, auth, fixture_json):
    start(client, auth, fixture_json)
    _, q = client.app.state.sse_hub.subscribe()
    post_turns(client, auth, fixture_json, [1, 2])
    end = fixture_json("call_end")
    end["turns_count"] = 9  # the runtime says 9, two are stored
    client.post(f"/v1/calls/{CALL}/end", json=end, headers=auth)
    events = drain(q, 3) + settle(q)
    assert [(n, d["turns_count"]) for n, d in events] == [("call.turn", 1), ("call.turn", 2), ("call.ended", 2)]


def test_call_end_count_is_stored_as_reported_but_a_difference_is_logged(client, auth, fixture_json, caplog):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [1, 2])
    end = fixture_json("call_end")
    end["turns_count"] = 5
    caplog.set_level(logging.WARNING)
    assert client.post(f"/v1/calls/{CALL}/end", json=end, headers=auth).status_code == 200
    assert "runtime reported 5 turns, 2 stored" in caplog.text
    with client.app.state.session_factory() as db:
        from resolvix_ops.orm import Call
        assert db.get(Call, CALL).turns_count == 5  # stored as sent
    assert count_of(client, auth) == 2  # shown as counted


def test_matching_end_count_logs_nothing(client, auth, fixture_json, caplog):
    start(client, auth, fixture_json)
    post_turns(client, auth, fixture_json, [1, 2])
    end = fixture_json("call_end")
    end["turns_count"] = 2
    caplog.set_level(logging.WARNING)
    client.post(f"/v1/calls/{CALL}/end", json=end, headers=auth)
    assert "runtime reported" not in caplog.text
