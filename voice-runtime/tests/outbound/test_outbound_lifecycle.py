"""Status callbacks (authentication, replay, ordering), cancellation honesty and the watchdog (maximum duration, stuck calls)."""
from __future__ import annotations

import asyncio

import pytest

from resolvix_runtime.outbound.models import CallStatus

from helpers import cb_token, create, headers

URL = "/v1/outbound-calls"
SID = "MOCKSID0001"


def callback(client, provider, token=None, **payload):
    payload.setdefault("CallSid", SID)
    return client.post(f"/v1/webhooks/exotel/{token or cb_token(provider)}", json=payload)


def status_of(client, call_id):
    return client.get(f"{URL}/{call_id}", headers=headers(key=None)).json()


# ------------------------------------------------------------------ webhook authentication
def test_unknown_token_is_404_and_changes_nothing(client, provider):
    call = create(client).json()
    r = callback(client, provider, token="not-a-real-token-0123456789", Status="completed")
    assert r.status_code == 404 and status_of(client, call["call_id"])["status"] == "dialing"


def test_callback_must_carry_the_calls_provider_sid(client, provider, svc):
    call = create(client).json()
    r = callback(client, provider, CallSid="SOMEONE-ELSES-SID", Status="completed")
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "sid_mismatch"
    assert callback(client, provider, CallSid="", Status="completed").status_code == 403
    assert status_of(client, call["call_id"])["status"] == "dialing"
    assert "callback_rejected" in [e["event"] for e in svc.store.audit_rows(call["call_id"])]


def test_one_calls_token_cannot_drive_another_call(client, provider):
    a = create(client, key="idem-a-0000001").json()
    b = create(client, to="+919812345678", key="idem-b-0000001").json()
    r = client.post(f"/v1/webhooks/exotel/{cb_token(provider, 0)}", json={"CallSid": "MOCKSID0002", "Status": "completed"})
    assert r.status_code == 403
    assert status_of(client, a["call_id"])["status"] == "dialing" and status_of(client, b["call_id"])["status"] == "dialing"


def test_callback_content_types_and_sizes(client, provider):
    create(client)
    token = cb_token(provider)
    form = client.post(f"/v1/webhooks/exotel/{token}", content="CallSid=MOCKSID0001&EventType=answered&Status=in-progress",
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert form.status_code == 200 and form.json()["applied"] is True
    assert client.post(f"/v1/webhooks/exotel/{token}", content="x", headers={"Content-Type": "text/plain"}).status_code == 422
    assert client.post(f"/v1/webhooks/exotel/{token}", content="{not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post(f"/v1/webhooks/exotel/{token}", content="[1,2]", headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post(f"/v1/webhooks/exotel/{token}", content="x" * 20000, headers={"Content-Type": "application/json"}).status_code == 422


# ------------------------------------------------------------------ lifecycle
def test_answered_then_completed_walks_the_lifecycle(client, provider):
    call = create(client).json()
    assert callback(client, provider, EventType="answered").json() == {"ok": True, "applied": True}
    assert status_of(client, call["call_id"])["status"] == "answered"
    done = callback(client, provider, Status="completed", ConversationDuration="42")
    assert done.json()["applied"] is True
    final = status_of(client, call["call_id"])
    assert final["status"] == "ended" and final["duration_s"] == 42 and final["ended_at"] and final["hangup_confirmed"] is True


@pytest.mark.parametrize("carrier,expected", [("busy", "busy"), ("no-answer", "no_answer"), ("failed", "failed"), ("canceled", "cancelled")])
def test_terminal_carrier_statuses_map_to_our_statuses(client, provider, carrier, expected):
    call = create(client).json()
    callback(client, provider, Status=carrier)
    final = status_of(client, call["call_id"])
    assert final["status"] == expected and final["ended_at"]
    if expected != "cancelled":
        assert final["failure_reason"] == f"carrier:{carrier}"


def test_duplicate_callbacks_are_idempotent(client, provider, svc):
    call = create(client).json()
    assert callback(client, provider, EventType="answered").json()["applied"] is True
    assert callback(client, provider, EventType="answered").json()["applied"] is False
    assert callback(client, provider, Status="completed").json()["applied"] is True
    again = callback(client, provider, Status="completed")
    assert again.status_code == 200 and again.json()["applied"] is False
    ends = [e for e in svc.store.audit_rows(call["call_id"]) if e["event"] == "terminal"]
    assert len(ends) == 1


def test_late_or_out_of_order_callbacks_never_move_a_call_backwards(client, provider):
    call = create(client).json()
    callback(client, provider, Status="busy")
    assert callback(client, provider, EventType="answered").json()["applied"] is False
    assert callback(client, provider, Status="completed").json()["applied"] is False
    assert status_of(client, call["call_id"])["status"] == "busy"


def test_unknown_statuses_are_ignored_not_guessed(client, provider):
    call = create(client).json()
    assert callback(client, provider, Status="ringing-ish").json()["applied"] is False
    assert callback(client, provider).json()["applied"] is False
    assert status_of(client, call["call_id"])["status"] == "dialing"


def test_a_finished_call_releases_its_in_memory_number(client, provider, svc):
    call = create(client).json()
    assert svc.number_for(call["call_id"]) == "+919876543210"
    callback(client, provider, Status="completed")
    assert svc.number_for(call["call_id"]) is None


# ------------------------------------------------------------------ cancellation (no hang-up is assumed)
def test_cancel_before_dial_is_immediate(svc, client, clock):
    row_id = "call-out-queuedtest1"
    svc.store.insert({"call_id": row_id, "requested_by": "admin@resolvix.test", "requester_role": "admin", "idempotency_key": "k" * 10,
                      "request_fp": "x", "dest_masked": "+91*******210", "dest_hash": "h", "country_code": "91", "lang": "hi",
                      "consent_basis": "opt_in", "consent_at": clock.t, "status": "queued", "created_at": clock.t, "provider": "mock"})
    r = client.post(f"{URL}/{row_id}/cancel", headers=headers(key=None))
    assert r.status_code == 202 and r.json()["status"] == "cancelled" and r.json()["failure_reason"] == "cancelled_before_dial"


def test_cancelling_a_live_call_only_requests_it_when_the_provider_cannot_hang_up(client, provider, svc):
    call = create(client).json()
    r = client.post(f"{URL}/{call['call_id']}/cancel", headers=headers(key=None))
    body = r.json()
    assert r.status_code == 202 and body["cancel_requested"] is True and body["hangup_confirmed"] is False
    assert body["status"] == "dialing"                          # NOT cancelled: nothing proves the phone call is over
    assert provider.hangups == ["MOCKSID0001"]
    again = client.post(f"{URL}/{call['call_id']}/cancel", headers=headers(key=None)).json()
    assert again["status"] == "dialing" and len(provider.hangups) == 2
    events = [e["event"] for e in svc.store.audit_rows(call["call_id"])]
    assert events.count("cancel_requested") == 1 and "hangup_attempt" in events


def test_cancelling_when_the_provider_confirms_the_hangup_ends_the_call(client, provider):
    provider.hangup_supported = provider.hangup_confirms = True
    call = create(client).json()
    body = client.post(f"{URL}/{call['call_id']}/cancel", headers=headers(key=None)).json()
    assert body["status"] == "cancelled" and body["hangup_confirmed"] is True


def test_the_carrier_report_after_a_cancel_request_is_still_authoritative(client, provider):
    call = create(client).json()
    client.post(f"{URL}/{call['call_id']}/cancel", headers=headers(key=None))
    callback(client, provider, Status="completed")
    assert status_of(client, call["call_id"])["status"] == "ended"


def test_cancelling_a_finished_call_is_a_no_op(client, provider):
    call = create(client).json()
    callback(client, provider, Status="busy")
    r = client.post(f"{URL}/{call['call_id']}/cancel", headers=headers(key=None))
    assert r.status_code == 202 and r.json()["status"] == "busy" and provider.hangups == []


# ------------------------------------------------------------------ watchdog
def tick(svc, now):
    return asyncio.run(svc.tick(now))


def test_maximum_duration_is_enforced_locally_and_recorded_honestly(client, provider, svc, clock):
    call = create(client).json()
    callback(client, provider, EventType="answered")
    clock.t += 121                                              # max_call_seconds is 120 in the test config
    assert tick(svc, clock.t) == 1
    mid = status_of(client, call["call_id"])
    assert mid["status"] == "answered" and mid["cancel_requested"] is True            # we asked, the carrier has not confirmed
    assert provider.hangups == ["MOCKSID0001"]
    clock.t += 16
    tick(svc, clock.t)
    final = status_of(client, call["call_id"])
    assert final["status"] == "ended" and final["failure_reason"] is None and final["outcome"] == "max_duration_enforced_locally"
    assert final["hangup_confirmed"] is False


def test_a_call_under_the_limit_is_left_alone(client, provider, svc, clock):
    call = create(client).json()
    callback(client, provider, EventType="answered")
    clock.t += 60
    assert tick(svc, clock.t) == 0 and status_of(client, call["call_id"])["status"] == "answered"


def test_a_call_the_carrier_never_reports_on_becomes_no_answer(client, provider, svc, clock):
    call = create(client).json()
    clock.t += 30 + 20 + 1                                       # ring timeout 30 s + 20 s grace
    tick(svc, clock.t)
    final = status_of(client, call["call_id"])
    assert final["status"] == "no_answer" and final["failure_reason"] == "no_carrier_report_after_ring_timeout"


def test_a_queued_call_that_never_dialled_is_failed(svc, client, clock):
    svc.store.insert({"call_id": "call-out-stuck000001", "requested_by": "a@b.co", "requester_role": "admin", "idempotency_key": "k" * 10,
                      "request_fp": "x", "dest_masked": "m", "dest_hash": "h2", "country_code": "91", "lang": "hi", "consent_basis": "opt_in",
                      "consent_at": clock.t, "status": "queued", "created_at": clock.t, "provider": "mock"})
    clock.t += 31
    tick(svc, clock.t)
    assert svc.store.get("call-out-stuck000001")["status"] == "failed"


def test_an_unconfirmed_cancel_is_closed_locally_after_a_grace_period(client, provider, svc, clock):
    call = create(client).json()
    client.post(f"{URL}/{call['call_id']}/cancel", headers=headers(key=None))
    clock.t += 16
    tick(svc, clock.t)
    final = status_of(client, call["call_id"])
    assert final["status"] == "cancelled" and final["failure_reason"] == "cancel_unconfirmed_by_carrier" and final["hangup_confirmed"] is False
