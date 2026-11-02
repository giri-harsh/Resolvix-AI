"""Contract test for the browser voice API (/api/voice). The contract is integration/VOICE_WEB_API.md."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import respx
from fastapi.testclient import TestClient

from resolvix_runtime.app import create_app
from resolvix_runtime.config import RuntimeSettings
from resolvix_runtime.sessions import get_session_registry

ORIGIN = "http://localhost:5174"


def _settings(**kw) -> RuntimeSettings:
    base = dict(port=8000, shim_api_key="k", service_key="svc", ops_api_url="http://ops.test", brain_impl="mock", telephony_mode="mock",
                voice_web_cors_origins=f"{ORIGIN},https://demo.example.org", voice_web_idle_timeout_s=30.0)
    base.update(kw)
    return RuntimeSettings(**base)


@pytest.fixture
def ops():
    with respx.mock(base_url="http://ops.test", assert_all_called=False) as mock:
        mock.post("/v1/calls").respond(201, json={"status": "created"})
        mock.post(path__regex=r"/v1/calls/.+/turns").respond(201, json={})
        mock.post(path__regex=r"/v1/calls/.+/end").respond(200, json={})
        mock.post("/v1/tickets").respond(201, json={"ticket_id": "EX-1042", "created": True})
        mock.post("/v1/notifications/sms").respond(202, json={"message_id": "m1", "status": "queued", "mode": "mock"})
        yield mock


def _client(**kw) -> TestClient:
    return TestClient(create_app(_settings(**kw)))


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def _end_calls(ops) -> list:
    return [c for c in ops.calls if c.request.url.path.endswith("/end")]


def test_config_and_start_call(ops):
    with _client() as c:
        cfg = c.get("/api/voice/config").json()
        assert cfg["api_version"] == 1 and cfg["streaming"] is True
        r = c.post("/api/voice/calls", json={})
        assert r.status_code == 201
        body = r.json()
        assert body["call_id"].startswith("call-voice-") and body["greeting_text"] and body["lang"]
        assert any(x.request.url.path == "/v1/calls" for x in ops.calls)             # call announced to ops
        # no body at all is fine too
        assert c.post("/api/voice/calls").status_code == 201


def test_turn_streams_sse_with_meta_speech_done(ops):
    with _client() as c:
        call_id = c.post("/api/voice/calls", json={}).json()["call_id"]
        r = c.post(f"/api/voice/calls/{call_id}/turns", json={"text": "what is the fee payment last date"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(r.text)
        names = [n for n, _ in events]
        assert names[0] == "meta" and names[-1] == "done" and "speech" in names
        speech = [d for n, d in events if n == "speech"]
        assert [s["index"] for s in speech] == list(range(len(speech)))
        meta = events[0][1]
        assert {"turn_index", "path", "tier", "lang", "interruptable"} <= set(meta)
        done = events[-1][1]
        assert done["turn_index"] == 1 and done["ticket_ids"] == [] and done["ended"] is False


def test_turn_json_mode_and_ticket_flow(ops):
    with _client() as c:
        call_id = c.post("/api/voice/calls", json={}).json()["call_id"]
        r = c.post(f"/api/voice/calls/{call_id}/turns?stream=false", json={"text": "bus route ka number kya hai"})
        assert r.status_code == 200
        body = r.json()
        assert body["reply_text"] and body["sentences"] and body["lang"] and body["path"]
        assert any(a["kind"] == "create_ticket" for a in body["actions"])
        assert body["ticket_ids"] == ["EX-1042"]
        for a in body["actions"]:
            assert "to_number" not in a and "vars" not in a                               # no phone numbers or SMS bodies reach the page


def test_end_call_posts_to_ops_once_and_is_idempotent(ops):
    with _client() as c:
        call_id = c.post("/api/voice/calls", json={}).json()["call_id"]
        c.post(f"/api/voice/calls/{call_id}/turns?stream=false", json={"text": "fees kab bharni hai"})
        r = c.post(f"/api/voice/calls/{call_id}/end")
        assert r.status_code == 200 and r.json()["status"] == "ended" and r.json()["outcome"] == "answered" and r.json()["ai_resolved"] is True
        assert len(_end_calls(ops)) == 1 and _end_calls(ops)[0].request.url.path == f"/v1/calls/{call_id}/end"
        again = c.post(f"/api/voice/calls/{call_id}/end")                                  # session is gone: stable 404, no second ops post
        assert again.status_code == 404 and again.json()["error"]["code"] == "not_found"
        assert len(_end_calls(ops)) == 1
        # a turn on an ended call is rejected
        t = c.post(f"/api/voice/calls/{call_id}/turns", json={"text": "hello"})
        assert t.status_code == 404


def test_zero_turn_call_ends_abandoned(ops):
    with _client() as c:
        call_id = c.post("/api/voice/calls", json={}).json()["call_id"]
        r = c.post(f"/api/voice/calls/{call_id}/end").json()
        assert r["outcome"] == "abandoned" and r["ai_resolved"] is False


def test_error_envelope_and_validation(ops):
    with _client() as c:
        r = c.post("/api/voice/calls/call-voice-nope/turns", json={"text": "hi"})
        assert r.status_code == 404 and set(r.json()["error"]) >= {"code", "message", "retryable"}
        call_id = c.post("/api/voice/calls", json={}).json()["call_id"]
        assert c.post(f"/api/voice/calls/{call_id}/turns", json={"text": ""}).json()["error"]["code"] == "validation_error"
        assert c.post(f"/api/voice/calls/{call_id}/turns", json={}).status_code == 422
        long = c.post(f"/api/voice/calls/{call_id}/turns", json={"text": "x" * 601})
        assert long.status_code == 422
        bad = c.post("/api/voice/calls", json={"caller_number": "98765"})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"
        ok = c.post("/api/voice/calls", json={"caller_number": "+919876543210"})
        assert ok.status_code == 201


def test_caller_number_can_be_disabled(ops):
    with _client(voice_web_allow_caller_number=False) as c:
        assert c.post("/api/voice/calls", json={"caller_number": "+919876543210"}).status_code == 422
        assert c.post("/api/voice/calls", json={}).status_code == 201


def test_cancel_is_safe_when_nothing_is_running(ops):
    with _client() as c:
        call_id = c.post("/api/voice/calls", json={}).json()["call_id"]
        r = c.post(f"/api/voice/calls/{call_id}/cancel")
        assert r.status_code == 200 and r.json() == {"call_id": call_id, "cancelled": False, "reason": "no_turn_in_progress"}
        assert c.post("/api/voice/calls/call-voice-nope/cancel").status_code == 404


def test_cors_allows_configured_origins_only(ops):
    with _client() as c:
        pre = c.options("/api/voice/calls", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "content-type,authorization"})
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == ORIGIN
        other = c.options("/api/voice/calls", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in other.headers
        r = c.post("/api/voice/calls", json={}, headers={"Origin": "https://demo.example.org"})
        assert r.headers["access-control-allow-origin"] == "https://demo.example.org"


def test_bearer_gate_when_key_configured_and_cors_still_visible(ops):
    with _client(voice_web_api_key="s3cret-voice-key-0123456789") as c:
        r = c.post("/api/voice/calls", json={}, headers={"Origin": ORIGIN})
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        assert r.headers["access-control-allow-origin"] == ORIGIN                           # browser can read the 401
        assert c.post("/api/voice/calls", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        good = c.post("/api/voice/calls", json={}, headers={"Authorization": "Bearer s3cret-voice-key-0123456789"})
        assert good.status_code == 201
        pre = c.options("/api/voice/calls", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
        assert pre.status_code == 200                                                       # preflight carries no credentials


def test_capacity_limit(ops):
    with _client(voice_web_max_calls=2) as c:
        assert c.post("/api/voice/calls", json={}).status_code == 201
        assert c.post("/api/voice/calls", json={}).status_code == 201
        r = c.post("/api/voice/calls", json={})
        assert r.status_code == 429 and r.json()["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_idle_calls_are_ended_by_the_server(ops):
    from resolvix_runtime.voice_api import VoiceWebApi
    from resolvix_runtime.ops_client import OpsClient
    from resolvix_runtime.brain_loader import reset_brain

    reset_brain()
    s = _settings(voice_web_idle_timeout_s=30.0)
    api = VoiceWebApi(s, OpsClient(s))
    app = api.build_app()
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        call_id = (await c.post("/calls", json={})).json()["call_id"]
        assert await api.sweep_idle() == 0                                                  # fresh call stays
        future = datetime.now(timezone.utc) + timedelta(seconds=31)
        assert await api.sweep_idle(now=future) == 1
        assert get_session_registry().get(call_id) is None
        ends = _end_calls(ops)
        assert len(ends) == 1 and json.loads(ends[0].request.content)["outcome"] == "abandoned"
