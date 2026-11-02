"""Shared constants and request helpers for the outbound tests."""
from __future__ import annotations

import json
from datetime import timedelta, timezone

from resolvix_runtime.outbound.provider import MockProvider

SERVICE_KEY = "unit-test-service-key-0123456789abcdef"
IST = timezone(timedelta(hours=5, minutes=30))
GOOD_NUMBER = "+919876543210"
OTHER_NUMBER = "+919812345678"
THIRD_NUMBER = "+917012345678"
CALLER_ID = "+918044556677"
IDEM = "idem-key-000001"


def headers(user="admin@resolvix.test", role="admin", key=IDEM, service_key=SERVICE_KEY):
    h = {"X-Service-Key": service_key, "X-On-Behalf-Of": user, "X-User-Role": role}
    if key:
        h["Idempotency-Key"] = key
    return h


def body(to=GOOD_NUMBER, lang="hinglish", confirmed=True, basis="customer_requested", note="asked for a callback"):
    return {"to": to, "lang": lang, "consent": {"confirmed": confirmed, "basis": basis, "note": note}}


def create(client, to=GOOD_NUMBER, key=IDEM, user="admin@resolvix.test", role="admin", **kw):
    return client.post("/v1/outbound-calls", json=body(to=to, **kw), headers=headers(user=user, role=role, key=key))


def ws_token(provider: MockProvider, index: int = -1) -> str:
    return provider.placed[index].stream_url.rsplit("/", 1)[1]


def cb_token(provider: MockProvider, index: int = -1) -> str:
    return provider.placed[index].status_callback_url.rsplit("/", 1)[1]


def start_event(stream_sid="ST1", call_sid="MOCKSID0001", custom=None):
    return json.dumps({"event": "start", "sequence_number": 1, "stream_sid": stream_sid,
                       "start": {"stream_sid": stream_sid, "call_sid": call_sid, "account_sid": "AC", "from": "x", "to": "y",
                                 "custom_parameters": custom or {}, "media_format": {"sample_rate": "8000"}}})
