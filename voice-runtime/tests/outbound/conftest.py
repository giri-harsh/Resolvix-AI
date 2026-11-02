"""Shared fixtures for the outbound-calling tests. Everything is offline: a socket guard fails the test on any non-loopback connection,
ops is mocked with respx (an unmatched request raises), the provider/ASR/TTS are the in-memory mocks."""
from __future__ import annotations

import socket
from datetime import datetime

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from resolvix_runtime.config import RuntimeSettings
from resolvix_runtime.ops_client import OpsClient
from resolvix_runtime.outbound import OutboundService, OutboundSettings
from resolvix_runtime.outbound.api import build_router
from resolvix_runtime.outbound.provider import MockProvider
from resolvix_runtime.outbound.speech import MockAsr, MockTts

from helpers import CALLER_ID, IST, SERVICE_KEY

class NetworkAttempt(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    """Fail loudly on any attempt to reach a non-loopback address. (Loopback stays allowed: the event loop uses a local socketpair.)"""
    attempts: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _check(address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0") and not str(host).startswith("\0"):
            attempts.append(str(host))
            raise NetworkAttempt(f"outbound tests must not use the network (tried {host})")

    monkeypatch.setattr(socket.socket, "connect", lambda self, address: (_check(address), real_connect(self, address))[1])
    monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: (_check(address), real_connect_ex(self, address))[1])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(NetworkAttempt("DNS lookup attempted")))
    yield attempts
    assert not attempts, f"network attempts: {attempts}"


@pytest.fixture
def clock():
    class Clock:
        t = datetime(2026, 11, 3, 12, 0, tzinfo=IST).timestamp()       # noon IST, inside the calling window

        def __call__(self) -> float:
            return self.t

        def set_ist(self, hour: int, minute: int = 0) -> None:
            self.t = datetime(2026, 11, 3, hour, minute, tzinfo=IST).timestamp()

    return Clock()


@pytest.fixture
def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings(service_key=SERVICE_KEY, shim_api_key="shim-test-key", ops_api_url="http://ops.test", brain_impl="mock",
                           telephony_mode="mock", public_base_url="http://localhost:8000")


@pytest.fixture
def cfg(tmp_path) -> OutboundSettings:
    return OutboundSettings(enabled=True, db_path=str(tmp_path / "outbound.db"), public_base_url="https://voice.example.test", caller_id=CALLER_ID,
                            max_call_seconds=120, ring_timeout_s=30, max_concurrent=3, per_user_hour=10, per_user_day=30, per_destination_day=2,
                            daily_seconds_cap=7200, duplicate_window_s=60)


@pytest.fixture
def ops_mock():
    with respx.mock(base_url="http://ops.test", assert_all_called=False) as mock:
        mock.post("/v1/calls").respond(201, json={"status": "created"})
        mock.post(path__regex=r"/v1/calls/.+/turns").respond(201, json={})
        mock.post(path__regex=r"/v1/calls/.+/end").respond(200, json={})
        mock.post(path__regex=r"/v1/tickets/.+/events").respond(201, json={})
        mock.post("/v1/tickets").respond(201, json={"ticket_id": "EX-1042", "created": True})
        mock.post("/v1/notifications/sms").respond(202, json={"message_id": "m1", "status": "queued", "mode": "mock"})
        yield mock


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def asr_log():
    return {}


@pytest.fixture
def tts_log():
    return {}


@pytest.fixture
def svc(runtime_settings, cfg, ops_mock, provider, clock, asr_log, tts_log):
    scripts: dict = {}
    asr_fail: dict = {}
    tts_fail: set = set()

    def asr_factory(call_id):
        a = MockAsr(script=list(scripts.get(call_id, scripts.get("*", []))), fail_on_chunk=asr_fail.get(call_id, asr_fail.get("*")))
        asr_log[call_id] = a
        return a

    def tts_factory(call_id):
        t = MockTts(fail=call_id in tts_fail or "*" in tts_fail)
        tts_log[call_id] = t
        return t

    service = OutboundService(runtime_settings, cfg, OpsClient(runtime_settings), provider=provider, asr_factory=asr_factory,
                              tts_factory=tts_factory, clock=clock)
    service.scripts, service.asr_fail, service.tts_fail = scripts, asr_fail, tts_fail     # e.g. service.scripts["*"] = ["hostel gate"]
    yield service
    service.store.close()


@pytest.fixture
def client(svc):
    app = FastAPI()
    app.include_router(build_router(svc))
    with TestClient(app) as c:
        yield c
