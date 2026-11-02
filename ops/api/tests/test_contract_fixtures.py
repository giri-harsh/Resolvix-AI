"""Every contracts/fixtures/ops fixture is either exercised against its endpoint or skipped with a stated reason."""
import pytest
from resolvix_contracts import (
    CallEnd, CallStart, ErrorEnvelope, GapCluster, GapCreate, KBSource, KBVersionInfo, SmsRequest, SmsResult, Ticket, TicketCreate, TicketCreated, TicketEvent, TicketPatch, TurnSummary,
)

from _m2 import CONTRACT_FIXTURES

EXERCISED = {
    "call_start": "POST /v1/calls",
    "call_end": "POST /v1/calls/{id}/end",
    "turn_summary": "POST /v1/calls/{id}/turns",
    "ticket_create": "POST /v1/tickets",
    "ticket_created": "response model of POST /v1/tickets",
    "ticket": "response model of GET/PATCH /v1/tickets/{id}",
    "ticket_event": "POST/GET /v1/tickets/{id}/events",
    "ticket_patch": "PATCH /v1/tickets/{id}",
    "error": "ErrorEnvelope shape of 404/422 responses",
    "sms_request": "POST /v1/notifications/sms",
    "sms_result": "response model of POST /v1/notifications/sms",
    "gap_create": "POST /v1/gaps",
    "gap_cluster": "response model of GET /v1/gaps/clusters",
    "kb_source": "KBSource body sent by POST /v1/kb/publish (live mode)",
    "kb_version_info": "response model of POST /v1/kb/publish",
}
SKIPPED = {
    "call_context": "A<->B brain protocol model, no ops-api endpoint",
    "call_start_plan": "A<->B brain protocol model, no ops-api endpoint",
}


def test_every_fixture_is_accounted_for():
    on_disk = {p.stem for p in CONTRACT_FIXTURES.glob("*.json")}
    assert on_disk == set(EXERCISED) | set(SKIPPED), on_disk ^ (set(EXERCISED) | set(SKIPPED))


@pytest.mark.parametrize("name,reason", sorted(SKIPPED.items()))
def test_fixture_out_of_m2_scope(name, reason):
    pytest.skip(f"{name}: {reason}")


def test_fixtures_are_valid_contract_models(fixture_json):
    CallStart.model_validate(fixture_json("call_start"))
    CallEnd.model_validate(fixture_json("call_end"))
    TurnSummary.model_validate(fixture_json("turn_summary"))
    TicketCreate.model_validate(fixture_json("ticket_create"))
    TicketCreated.model_validate(fixture_json("ticket_created"))
    Ticket.model_validate(fixture_json("ticket"))
    TicketEvent.model_validate(fixture_json("ticket_event"))
    TicketPatch.model_validate(fixture_json("ticket_patch"))
    ErrorEnvelope.model_validate(fixture_json("error"))
    SmsRequest.model_validate(fixture_json("sms_request"))
    SmsResult.model_validate(fixture_json("sms_result"))
    GapCreate.model_validate(fixture_json("gap_create"))
    GapCluster.model_validate(fixture_json("gap_cluster"))
    KBSource.model_validate(fixture_json("kb_source"))
    KBVersionInfo.model_validate(fixture_json("kb_version_info"))


def test_gap_flow_with_fixtures(client, auth, fixture_json):
    r = client.post("/v1/gaps", json=fixture_json("gap_create"), headers=auth)
    assert r.status_code == 201
    items = client.get("/v1/gaps/clusters", headers=auth).json()["items"]
    assert len(items) == 1
    cluster = GapCluster.model_validate(items[0])
    assert set(items[0]) == set(fixture_json("gap_cluster")) and cluster.count == 1


def test_sms_flow_with_fixtures(sms_client, auth, fixture_json):
    r = sms_client.post("/v1/notifications/sms", json=fixture_json("sms_request"), headers=auth)
    assert r.status_code == 202
    result = SmsResult.model_validate(r.json())
    assert set(r.json()) == set(fixture_json("sms_result"))
    assert result.mode == fixture_json("sms_result")["mode"] == "mock"
    r2 = sms_client.post("/v1/notifications/sms", json=fixture_json("sms_request"), headers=auth)
    assert r2.status_code == 200 and SmsResult.model_validate(r2.json()) == result


def test_calls_flow_with_fixtures(client, auth, fixture_json):
    cs = fixture_json("call_start")
    r = client.post("/v1/calls", json=cs, headers=auth)
    assert r.status_code == 201

    turn = fixture_json("turn_summary")
    r = client.post(f"/v1/calls/{cs['call_id']}/turns", json=turn, headers=auth)
    assert r.status_code == 201
    TurnSummary.model_validate(r.json())

    r = client.post(f"/v1/calls/{cs['call_id']}/end", json=fixture_json("call_end"), headers=auth)
    assert r.status_code == 200

    detail = client.get(f"/v1/calls/{cs['call_id']}", headers=auth).json()
    allowed = set(CallStart.model_fields) | set(CallEnd.model_fields)
    assert set(detail["call"]) == allowed
    assert detail["call"]["outcome"] == "transferred" and detail["call"]["turns_count"] == 1  # one stored turn (index 2); CallEnd said 3
    for t in detail["turns"]:
        TurnSummary.model_validate(t)
    assert detail["turns"][0]["priority_reason"] == turn["priority_reason"]

    listed = client.get("/v1/calls", headers=auth).json()["items"]
    assert set(listed[0]) == allowed | {"last_turn"}  # additive: latest turn for the live board
    assert listed[0]["last_turn"]["path"] == turn["path"] and listed[0]["last_turn"]["turn_index"] == 2


def test_tickets_flow_with_fixtures(client, auth, fixture_json):
    tc = fixture_json("ticket_create")
    r = client.post("/v1/tickets", json=tc, headers=auth)
    assert r.status_code == 201
    created = TicketCreated.model_validate(r.json())
    assert created.created is True
    assert set(r.json()) == set(fixture_json("ticket_created"))

    r = client.get(f"/v1/tickets/{created.ticket_id}", headers=auth)
    ticket = Ticket.model_validate(r.json())
    assert set(r.json()) == set(fixture_json("ticket"))  # same field set as the contract example
    assert ticket.priority == tc["priority"] and ticket.department == tc["department"]
    assert ticket.collected_fields == tc["collected_fields"] and ticket.status == "open"

    for item in client.get("/v1/tickets", headers=auth).json()["items"]:
        Ticket.model_validate(item)

    r = client.patch(f"/v1/tickets/{created.ticket_id}", json=fixture_json("ticket_patch"), headers=auth)
    assert r.status_code == 200
    Ticket.model_validate(r.json())

    ev = fixture_json("ticket_event")
    assert client.post(f"/v1/tickets/{created.ticket_id}/events", json=ev, headers=auth).status_code == 201
    items = client.get(f"/v1/tickets/{created.ticket_id}/events", headers=auth).json()["items"]
    assert len(items) >= 1
    for item in items:
        TicketEvent.model_validate(item)
    from resolvix_ops.routers.tickets_routes import mask_payload
    # numbers inside event payloads are returned masked (REVIEW M8); the masked form still validates as a TicketEvent
    assert any(i["kind"] == ev["kind"] and i["payload"] == mask_payload(ev["payload"]) for i in items)


def test_error_responses_match_error_fixture_shape(client, auth, fixture_json):
    shape = set(fixture_json("error")["error"])
    for r in (client.get("/v1/tickets/XX-0000", headers=auth), client.post("/v1/calls", json={}, headers=auth),
              client.get("/v1/tickets")):
        env = ErrorEnvelope.model_validate(r.json())
        assert set(r.json()["error"]) == shape
        assert env.error.code in {"not_found", "validation_error", "unauthorized"}
