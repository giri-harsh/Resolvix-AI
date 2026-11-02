"""Configuration, default-off behaviour, the planned Exotel request shape, and the mock-only guarantees of this build."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from resolvix_runtime.app import create_app
from resolvix_runtime.config import RuntimeSettings
from resolvix_runtime.outbound import OutboundSettings
from resolvix_runtime.outbound.provider import PlaceCall, build_connect_form

from helpers import SERVICE_KEY, headers

PKG = Path(__file__).resolve().parents[2] / "resolvix_runtime" / "outbound"


def test_settings_defaults_are_safe(monkeypatch):
    for name in [n for n in __import__("os").environ if n.startswith("OUTBOUND_")] + ["EXOTEL_CALLER_ID"]:
        monkeypatch.delenv(name, raising=False)
    s = OutboundSettings.from_env()
    assert s.enabled is False and s.provider == "mock" and s.allowed_country_codes == ("91",) and s.enforce_hours is True
    assert s.max_call_seconds == 180 and s.per_destination_day == 2 and s.max_concurrent == 3
    assert s.db_path.replace("\\", "/").endswith("integration/.run/outbound.db")           # git-ignored location


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("OUTBOUND_ENABLED", "true")
    monkeypatch.setenv("OUTBOUND_ALLOWED_COUNTRY_CODES", "+91, 971")
    monkeypatch.setenv("OUTBOUND_MAX_CALL_SECONDS", "90")
    monkeypatch.setenv("EXOTEL_CALLER_ID", "+918044556677")
    s = OutboundSettings.from_env()
    assert s.enabled and s.allowed_country_codes == ("91", "971") and s.max_call_seconds == 90 and s.caller_id == "+918044556677"


def test_the_build_never_reads_exotel_credentials_or_imports_a_network_client():
    text = "\n".join(p.read_text(encoding="utf-8") for p in PKG.glob("*.py"))
    for name in ("EXOTEL_API_KEY", "EXOTEL_API_TOKEN", "EXOTEL_ACCOUNT_SID", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "SARVAM_API_KEY"):
        assert not re.search(rf"[\"']{name}[\"']", text), name            # never looked up in the environment (a docstring may mention them)
    provider_src = (PKG / "provider.py").read_text(encoding="utf-8")
    for imp in ("import httpx", "import requests", "import socket", "import aiohttp", "import websockets", "urllib.request"):
        assert imp not in provider_src, imp
    for module in ("speech.py", "audio.py", "validation.py", "store.py"):
        src = (PKG / module).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|from)\s+(httpx|requests|aiohttp|websockets|socket)\b", src, re.M), module


def test_connect_form_has_the_documented_fields_and_no_credentials():
    req = PlaceCall(call_id="call-out-x", to="+919876543210", caller_id="+918044556677", stream_url="wss://h.example/ws/exotel/tok",
                    status_callback_url="https://h.example/v1/webhooks/exotel/tok2", time_limit_s=120, ring_timeout_s=30, custom_field="call-out-x")
    form = build_connect_form(req)
    assert form["StreamType"] == "bidirectional" and form["StreamUrl"].startswith("wss://") and form["TimeLimit"] == "120" and form["TimeOut"] == "30"
    assert form["StatusCallbackEvents"] == "answered,terminal" and form["CustomField"] == "call-out-x" and form["CallerId"] == "+918044556677"
    assert not any(k.lower() in ("apikey", "api_key", "token", "password", "authorization") for k in form)


def test_the_app_mounts_the_routes_but_outbound_calling_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTBOUND_ENABLED", raising=False)
    db = tmp_path / "must-not-exist.db"
    monkeypatch.setenv("OUTBOUND_DB_PATH", str(db))
    app = create_app(RuntimeSettings(service_key=SERVICE_KEY, brain_impl="mock", telephony_mode="mock"))
    with TestClient(app) as c:
        r = c.post("/v1/outbound-calls", json={"to": "+919876543210"}, headers=headers())
        assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "outbound_disabled"
        assert c.get("/v1/outbound-calls", headers=headers(key=None)).status_code == 403
        assert c.post("/v1/webhooks/exotel/anything", json={}).status_code == 404
        assert c.get("/healthz").status_code == 200                                         # the existing runtime is unaffected
    assert not db.exists()                                                                   # nothing is created while disabled


def test_existing_runtime_routes_are_unchanged():
    paths = {getattr(r, "path", "") for r in create_app(RuntimeSettings(service_key=SERVICE_KEY, brain_impl="mock")).routes}
    for expected in ("/healthz", "/v1/chat/completions", "/api/sessions/web", "/api/test/turn", "/call", "/api/voice"):
        assert any(p == expected or p.startswith(expected.split("{")[0]) for p in paths), expected
