"""The /v1/outbound-calls API: authentication, consent, limits, idempotency, duplicates, provider failures, visibility, audit hygiene."""
from __future__ import annotations

import json

import pytest

from resolvix_runtime.outbound.provider import ProviderError

from helpers import CALLER_ID, GOOD_NUMBER, IDEM, OTHER_NUMBER, SERVICE_KEY, THIRD_NUMBER, body, create, cb_token, headers, ws_token

URL = "/v1/outbound-calls"


# ------------------------------------------------------------------ authentication and authorization
def test_missing_or_wrong_service_key_is_401_and_never_reaches_the_provider(client, provider):
    for h in ({}, {"X-Service-Key": "wrong-key-wrong-key-wrong-key-1234"}, {"Authorization": f"Bearer {SERVICE_KEY}"}):
        r = client.post(URL, json=body(), headers={**h, "Idempotency-Key": IDEM})
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert provider.placed == []


def test_user_and_role_are_required_and_checked(client, provider):
    assert client.post(URL, json=body(), headers={"X-Service-Key": SERVICE_KEY, "X-User-Role": "admin", "Idempotency-Key": IDEM}).status_code == 401
    bad_user = headers(user="not-an-email")
    assert client.post(URL, json=body(), headers=bad_user).status_code == 401
    for role in ("viewer", "", "root", "ADMINISTRATOR"):
        r = client.post(URL, json=body(), headers=headers(role=role))
        assert r.status_code == 403 and r.json()["error"]["code"] == "unauthorized"
    assert provider.placed == []
    assert create(client, role="staff", user="staff@resolvix.test").status_code == 202


def test_a_weak_server_key_refuses_everything(client, svc):
    svc.rs.service_key = "change-me"
    r = client.post(URL, json=body(), headers=headers(service_key="change-me"))
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"


def test_outbound_calling_is_off_unless_enabled(client, svc, provider):
    svc.cfg.enabled = False
    r = create(client)
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "outbound_disabled" and provider.placed == []
    assert client.get(f"{URL}/anything", headers=headers()).status_code == 403
    assert client.post("/v1/webhooks/exotel/whatever", json={}).status_code == 404


# ------------------------------------------------------------------ happy path and response hygiene
def test_successful_initiation_with_the_mock_provider(client, provider, svc):
    r = create(client)
    assert r.status_code == 202 and r.headers["location"].startswith(URL + "/call-out-")
    call = r.json()
    assert call["status"] == "dialing" and call["destination_masked"] == "+91*******210" and call["provider"] == {"name": "mock", "sid": "MOCKSID0001"}
    assert call["cost"] is None and call["cancel_requested"] is False and call["hangup_confirmed"] is False
    assert len(provider.placed) == 1
    placed = provider.placed[0]
    assert placed.to == GOOD_NUMBER and placed.caller_id == CALLER_ID and placed.time_limit_s == 120 and placed.ring_timeout_s == 30
    assert placed.stream_url.startswith("wss://voice.example.test/ws/exotel/") and placed.status_callback_url.startswith("https://voice.example.test/v1/webhooks/exotel/")
    assert placed.custom_field == call["call_id"]
    text = json.dumps(call)
    assert GOOD_NUMBER not in text and ws_token(provider) not in text and cb_token(provider) not in text
    got = client.get(f"{URL}/{call['call_id']}", headers=headers()).json()
    assert got == call


def test_two_calls_get_distinct_ids_and_tokens(client, provider):
    a = create(client, key="idem-key-000001").json()
    b = create(client, to=OTHER_NUMBER, key="idem-key-000002").json()
    assert a["call_id"] != b["call_id"] and ws_token(provider, 0) != ws_token(provider, 1) and cb_token(provider, 0) != cb_token(provider, 1)


# ------------------------------------------------------------------ validation, consent, compliance
def test_invalid_numbers_are_rejected_without_calling_the_provider(client, provider):
    for i, number in enumerate(["abc", "9876543210", "+91 98765 43210", "+91123"]):
        r = create(client, to=number, key=f"idem-bad-{i:04d}x")
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert provider.placed == []


def test_missing_or_false_consent_blocks_the_call(client, provider):
    r = create(client, confirmed=False, key="idem-consent-0001")
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "consent_required"
    raw = {"to": GOOD_NUMBER}
    assert client.post(URL, json=raw, headers=headers(key="idem-consent-0002")).status_code == 422
    raw = {"to": GOOD_NUMBER, "consent": {"confirmed": True}}
    assert client.post(URL, json=raw, headers=headers(key="idem-consent-0003")).status_code == 422          # basis is required
    raw = {"to": GOOD_NUMBER, "consent": {"confirmed": True, "basis": "because_i_said_so"}}
    assert client.post(URL, json=raw, headers=headers(key="idem-consent-0004")).status_code == 422
    assert provider.placed == []


def test_the_caller_cannot_choose_a_purpose_or_extra_fields(client, provider):
    for extra in ({"purpose": "transactional"}, {"category": "service"}, {"enforce_hours": False}, {"caller_id": "+911234567890"}):
        r = client.post(URL, json={**body(), **extra}, headers=headers(key="idem-extra-0001"))
        assert r.status_code == 422, extra
    assert provider.placed == []


def test_emergency_and_unapproved_destinations_are_refused(client, provider):
    for i, number in enumerate(["+91112", "+91100", "+91108", "+14155550123", CALLER_ID]):
        r = create(client, to=number, key=f"idem-emerg-{i:04d}")
        assert r.status_code in (403, 422), number
    assert provider.placed == []


def test_calling_hours_are_enforced_server_side(client, provider, clock):
    clock.set_ist(22, 15)
    r = create(client, key="idem-hours-0001")
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "outside_calling_hours" and provider.placed == []
    clock.set_ist(10, 0)
    assert create(client, key="idem-hours-0002").status_code == 202


# ------------------------------------------------------------------ idempotency and duplicates
def test_same_idempotency_key_replays_without_a_second_call(client, provider):
    first = create(client)
    second = create(client)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["call_id"] == second.json()["call_id"] and len(provider.placed) == 1


def test_idempotency_key_reuse_with_a_different_request_is_a_conflict(client, provider):
    create(client)
    r = create(client, to=OTHER_NUMBER)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "idempotency_key_reuse" and len(provider.placed) == 1


def test_idempotency_keys_are_scoped_per_user(client, provider):
    assert create(client, user="a@resolvix.test", key="shared-key-0001").status_code == 202
    assert create(client, user="b@resolvix.test", to=OTHER_NUMBER, key="shared-key-0001").status_code == 202
    assert len(provider.placed) == 2


def test_an_idempotency_key_is_required(client, provider):
    r = client.post(URL, json=body(), headers=headers(key=None))
    assert r.status_code == 422 and r.json()["error"]["details"]["field"] == "Idempotency-Key"
    assert client.post(URL, json=body(), headers=headers(key="short")).status_code == 422


def test_a_second_request_for_the_same_number_is_a_duplicate(client, provider):
    create(client, key="idem-dup-000001")
    r = create(client, key="idem-dup-000002", user="other@resolvix.test")
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "duplicate_call" and len(provider.placed) == 1


def test_two_simultaneous_submissions_place_exactly_one_call(svc, provider):
    import asyncio
    from resolvix_runtime.outbound.models import OutboundCallRequest

    req = OutboundCallRequest.model_validate(body())

    async def go():
        return await asyncio.gather(svc.create_call("a@resolvix.test", "admin", "idem-race-0001", req),
                                    svc.create_call("a@resolvix.test", "admin", "idem-race-0002", req), return_exceptions=True)

    results = asyncio.run(go())
    assert len(provider.placed) == 1 and sum(isinstance(r, Exception) for r in results) == 1


# ------------------------------------------------------------------ rate limits
def test_per_user_hourly_limit(client, svc, provider):
    svc.cfg.per_user_hour, svc.cfg.max_concurrent, svc.cfg.per_destination_day = 2, 10, 5
    numbers = [GOOD_NUMBER, OTHER_NUMBER, THIRD_NUMBER]
    codes = [create(client, to=n, key=f"idem-hour-{i:04d}x").status_code for i, n in enumerate(numbers)]
    assert codes == [202, 202, 429]
    r = create(client, to="+919911223344", key="idem-hour-0009x")
    assert r.status_code == 429 and r.headers["retry-after"] and r.json()["error"]["retryable"] is True and r.json()["error"]["details"]["reason"] == "per_user_hour"


def test_the_window_slides(client, svc, clock):
    svc.cfg.per_user_hour, svc.cfg.max_concurrent = 1, 10
    assert create(client, key="idem-slide-0001").status_code == 202
    assert create(client, to=OTHER_NUMBER, key="idem-slide-0002").status_code == 429
    clock.t += 3601
    assert create(client, to=OTHER_NUMBER, key="idem-slide-0003").status_code == 202


def test_per_destination_daily_limit(client, svc, clock):
    svc.cfg.per_destination_day, svc.cfg.duplicate_window_s, svc.cfg.max_concurrent = 1, 0, 10
    first = create(client, key="idem-dest-00001").json()
    from resolvix_runtime.outbound.models import CallStatus

    svc.store.set_status(first["call_id"], CallStatus.failed, clock())
    clock.t += 5
    r = create(client, key="idem-dest-00002")
    assert r.status_code == 429 and r.json()["error"]["details"]["reason"] == "per_destination_day"


def test_global_concurrency_limit(client, svc):
    svc.cfg.max_concurrent = 2
    codes = [create(client, to=n, key=f"idem-conc-{i:04d}x").status_code for i, n in enumerate([GOOD_NUMBER, OTHER_NUMBER, THIRD_NUMBER])]
    assert codes == [202, 202, 429]


def test_daily_call_time_cap(client, svc):
    svc.cfg.daily_seconds_cap, svc.cfg.max_call_seconds = 200, 120
    assert create(client, key="idem-cap-000001").status_code == 202
    r = create(client, to=OTHER_NUMBER, key="idem-cap-000002")
    assert r.status_code == 429 and r.json()["error"]["details"]["reason"] == "daily_cap"


# ------------------------------------------------------------------ provider failures
@pytest.mark.parametrize("retryable", [True, False])
def test_provider_failure_marks_the_call_failed_and_is_audited(client, svc, provider, retryable):
    provider.fail_with = ProviderError("exotel unavailable (mock)", retryable=retryable)
    r = create(client)
    assert r.status_code == 202 and r.json()["status"] == "failed" and r.json()["failure_reason"].startswith("provider_error")
    call_id = r.json()["call_id"]
    assert "provider_error" in [e["event"] for e in svc.store.audit_rows(call_id)]


def test_the_real_exotel_provider_is_disabled_in_this_build(runtime_settings, cfg, ops_mock, clock):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from resolvix_runtime.ops_client import OpsClient
    from resolvix_runtime.outbound import OutboundService
    from resolvix_runtime.outbound.api import build_router

    cfg.provider = "exotel"
    s = OutboundService(runtime_settings, cfg, OpsClient(runtime_settings), clock=clock)
    app = FastAPI()
    app.include_router(build_router(s))
    with TestClient(app) as c:
        r = create(c)
    assert r.json()["status"] == "failed" and "disabled in this build" in r.json()["failure_reason"]
    s.store.close()


# ------------------------------------------------------------------ visibility, history
def test_staff_only_see_their_own_calls(client):
    mine = create(client, user="staff@resolvix.test", role="staff", key="idem-vis-000001").json()
    other = create(client, to=OTHER_NUMBER, user="boss@resolvix.test", role="admin", key="idem-vis-000002").json()
    staff = headers(user="staff@resolvix.test", role="staff", key=None)
    assert client.get(f"{URL}/{mine['call_id']}", headers=staff).status_code == 200
    assert client.get(f"{URL}/{other['call_id']}", headers=staff).status_code == 404
    assert client.post(f"{URL}/{other['call_id']}/cancel", headers=staff).status_code == 404
    ids = [c["call_id"] for c in client.get(URL, headers=staff).json()["items"]]
    assert ids == [mine["call_id"]]
    admin_ids = {c["call_id"] for c in client.get(URL, headers=headers(key=None)).json()["items"]}
    assert admin_ids == {mine["call_id"], other["call_id"]}
    assert client.get(f"{URL}/call-out-unknown", headers=headers(key=None)).status_code == 404


def test_history_is_newest_first_filterable_and_paginated(client, svc, clock):
    svc.cfg.max_concurrent, svc.cfg.per_user_hour, svc.cfg.per_destination_day = 20, 50, 5
    ids = []
    for i in range(5):
        clock.t += 10
        ids.append(create(client, to=f"+9198765432{i:02d}", key=f"idem-hist-{i:04d}x").json()["call_id"])
    page = client.get(f"{URL}?limit=2", headers=headers(key=None)).json()
    assert [c["call_id"] for c in page["items"]] == ids[::-1][:2] and page["next_before"] is not None
    nxt = client.get(f"{URL}?limit=2&before={page['next_before']}", headers=headers(key=None)).json()
    assert [c["call_id"] for c in nxt["items"]] == ids[::-1][2:4]
    assert client.get(f"{URL}?status=dialing", headers=headers(key=None)).json()["items"]
    assert client.get(f"{URL}?status=nonsense", headers=headers(key=None)).status_code == 422
    assert len(client.get(f"{URL}?limit=1000", headers=headers(key=None)).json()["items"]) == 5


# ------------------------------------------------------------------ audit hygiene
def test_audit_log_and_logs_contain_no_full_numbers_or_tokens(client, svc, provider, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    call = create(client).json()
    client.post(f"/v1/webhooks/exotel/{cb_token(provider)}", json={"CallSid": "MOCKSID0001", "EventType": "answered"})
    rows = svc.store.audit_rows(call["call_id"])
    runtime_logs = " | ".join(r.getMessage() for r in caplog.records if r.name.startswith("resolvix_runtime"))     # the test client's own httpx log is not ours
    blob = json.dumps(rows) + runtime_logs + json.dumps(call)
    assert [r["event"] for r in rows][:2] == ["created", "dialing"]
    for secret in (GOOD_NUMBER, "9876543210", ws_token(provider), cb_token(provider), SERVICE_KEY):
        assert secret not in blob, secret
    row = svc.store.get(call["call_id"])
    assert GOOD_NUMBER not in json.dumps(row, default=str) and ws_token(provider) not in json.dumps(row, default=str)
    assert row["consent_basis"] == "customer_requested" and row["consent_at"] and row["requested_by"] == "admin@resolvix.test"


def test_consent_notes_are_purged_after_the_retention_period(client, svc, clock):
    call = create(client).json()
    assert svc.store.get(call["call_id"])["consent_note"] == "asked for a callback"
    assert svc.store.purge_consent_notes(clock.t - 1) == 0
    assert svc.store.purge_consent_notes(clock.t + 1) == 1
    row = svc.store.get(call["call_id"])
    assert row["consent_note"] == "" and row["consent_basis"] == "customer_requested"
