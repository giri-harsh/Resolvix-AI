"""Phase 1.2: session authentication, entry key, CSRF, expiry, limits, streaming under auth and minimal health for the voice-web proxy mode."""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_proxy  # noqa: E402
from test_voice_proxy import Server, GOOD_KEY  # noqa: E402

CALL = "call-voice-0123456789"
ENTRY = "entry-key-for-tests-0123456789"
STATE_CHANGING = [
    ("/api/voice/calls", {}), (f"/api/voice/calls/{CALL}/turns", {"text": "hi"}), (f"/api/voice/calls/{CALL}/cancel", None),
    (f"/api/voice/calls/{CALL}/end", None), ("/api/test/turn", {"text": "hi"}), ("/api/test/end", {"call_id": "c1"}),
]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Upstream:
    def __init__(self):
        self.calls: list[dict] = []
        self.gate = threading.Event()
        self.app = Starlette()
        self.app.add_route("/{path:path}", self.any, methods=["GET", "POST"])

    async def any(self, request: Request):
        body = await request.body()
        self.calls.append({"method": request.method, "path": request.url.path, "headers": {k.lower(): v for k, v in request.headers.items()}, "body": body})
        path = request.url.path
        if path == "/healthz":
            return JSONResponse({"status": "ok", "brain": {"embedder": "e5", "llm": "openai/gpt-oss-20b", "kb_version": "abc", "llm_mode": "live"},
                                 "ops_api": "ok", "agora": "ok", "contract_version": "1.0.0"})
        if path == "/v1/chat/completions":
            if request.headers.get("authorization") != GOOD_KEY:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return JSONResponse({"ok": True})
        if path.endswith("/turns"):
            gate = self.gate

            async def sse():
                yield b"event: speech\ndata: {\"index\":0}\n\n"
                for _ in range(100):                       # wait (max 5 s) until the test has received the first chunk
                    if gate.is_set():
                        break
                    import asyncio
                    await asyncio.sleep(0.05)
                yield b"event: done\ndata: {}\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream")
        if path == "/api/voice/calls":
            return JSONResponse({"call_id": CALL}, status_code=201)
        if path.startswith("/voice"):
            return JSONResponse({"page": True})
        return JSONResponse({"ok": True})


def make(**kw):
    up = Upstream()
    clock = kw.pop("clock", None) or Clock()
    srv = Server(up.app)
    srv.__enter__()
    defaults = dict(voice_web=True, rate_per_min=1000, session_secret=b"unit-test-secret-0123456789abcdef", clock=clock)
    defaults.update(kw)
    app = voice_proxy.create_app(f"http://127.0.0.1:{srv.port}", **defaults)
    prox = Server(app)
    prox.__enter__()
    return up, clock, srv, prox


@pytest.fixture()
def stack():
    up, clock, srv, prox = make()
    yield up, clock, prox.port
    prox.__exit__(None, None, None)
    srv.__exit__(None, None, None)


@pytest.fixture()
def keyed():
    up, clock, srv, prox = make(entry_key=ENTRY)
    yield up, clock, prox.port
    prox.__exit__(None, None, None)
    srv.__exit__(None, None, None)


def url(port, path):
    return f"http://127.0.0.1:{port}{path}"


def origin(port):
    return {"Origin": f"http://127.0.0.1:{port}"}


@contextlib.contextmanager
def browser(port, key=None):
    """A browser stand-in: loads the page (gets the session cookie) and sends a same-origin Origin on every request."""
    with httpx.Client(headers=origin(port), timeout=10) as c:
        r = c.get(url(port, "/voice/" + (f"?k={key}" if key else "")), follow_redirects=True)
        assert r.status_code == 200
        yield c


# ------------------------------------------------------------------------------------------------ unauthenticated
@pytest.mark.parametrize("path,body", STATE_CHANGING)
def test_unauthenticated_state_changing_requests_are_401_and_never_forwarded(stack, path, body):
    up, _, port = stack
    r = httpx.post(url(port, path), json=body, headers=origin(port))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    assert up.calls == []


def test_a_forged_or_tampered_cookie_is_rejected(stack):
    up, clock, port = stack
    good = voice_proxy.SessionAuth(b"unit-test-secret-0123456789abcdef", 1200, 14400, clock).issue()
    other = voice_proxy.SessionAuth(b"another-secret-another-secret-00", 1200, 14400, clock).issue()
    payload, sig = good.split(".")
    flipped = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB") + "." + sig
    for token in (other, flipped, good + "x", "garbage", payload, ".", ""):
        r = httpx.post(url(port, "/api/test/turn"), json={"text": "x"}, headers={**origin(port), "Cookie": f"rx_voice={token}"})
        assert r.status_code == 401, token
    assert up.calls == []


# ------------------------------------------------------------------------------------------------ authenticated
def test_open_mode_page_load_grants_a_cookie_that_is_httponly_samesite_strict(stack):
    _, _, port = stack
    r = httpx.get(url(port, "/voice/"))
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("rx_voice=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie                                                                # plain http on loopback
    https = httpx.get(url(port, "/voice/"), headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in https.headers["set-cookie"]                                              # behind the tunnel


def test_authenticated_flow_and_cookie_never_reaches_the_runtime(stack):
    up, _, port = stack
    with browser(port) as c:
        assert c.post(url(port, "/api/voice/calls")).status_code == 201
        assert c.post(url(port, f"/api/voice/calls/{CALL}/turns?stream=false"), json={"text": "hi"}).status_code == 200
        assert c.post(url(port, f"/api/voice/calls/{CALL}/end")).status_code == 200
        assert c.post(url(port, "/api/test/turn"), json={"text": "hi"}).status_code == 200
        assert c.post(url(port, "/api/test/end"), json={"call_id": "c1"}).status_code == 200
    assert all("cookie" not in call["headers"] for call in up.calls)


def test_a_call_belongs_to_the_session_that_created_it(stack):
    up, _, port = stack
    with browser(port) as owner, browser(port) as other:
        assert owner.post(url(port, "/api/voice/calls")).status_code == 201
        n = len(up.calls)
        for sub in ("turns", "cancel", "end"):
            r = other.post(url(port, f"/api/voice/calls/{CALL}/{sub}"), json={"text": "x"})
            assert r.status_code == 403 and r.json()["error"]["code"] == "not_your_call"
        assert len(up.calls) == n
        assert owner.post(url(port, f"/api/voice/calls/{CALL}/end")).status_code == 200


# ------------------------------------------------------------------------------------------------ entry key
def test_entry_key_is_required_to_get_a_session(keyed):
    up, _, port = keyed
    assert httpx.get(url(port, "/voice/")).json()["error"]["code"] == "session_required"
    assert httpx.get(url(port, "/voice/style.css")).status_code == 401
    assert httpx.get(url(port, "/voice/?k=wrong-key-wrong-key-123")).status_code == 403
    assert httpx.get(url(port, "/voice/?k=short")).status_code == 400
    assert httpx.get(url(port, "/voice/?k=" + ENTRY.replace("e", "%65")), follow_redirects=False).status_code in (303, 400, 403)
    assert up.calls == []
    r = httpx.get(url(port, f"/voice/?k={ENTRY}"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/voice/" and ENTRY not in r.headers["location"]
    assert "rx_voice=" in r.headers["set-cookie"] and ENTRY not in r.headers["set-cookie"]
    with browser(port, ENTRY) as c:
        assert c.get(url(port, "/voice/style.css")).status_code == 200
        assert c.post(url(port, "/api/voice/calls")).status_code == 201


def test_the_entry_key_is_only_valid_on_the_page_url(keyed):
    up, _, port = keyed
    assert httpx.get(url(port, f"/voice/style.css?k={ENTRY}")).status_code == 400
    assert httpx.post(url(port, f"/api/voice/calls?k={ENTRY}"), headers=origin(port)).status_code == 400
    assert up.calls == []


def test_wrong_entry_keys_are_rate_limited_per_ip():
    up, clock, srv, prox = make(entry_key=ENTRY, failures_per_ip_per_min=3)
    try:
        codes = [httpx.get(url(prox.port, f"/voice/?k=wrong-key-wrong-key-{i:03d}")).status_code for i in range(5)]
        assert codes == [403, 403, 403, 429, 429]
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


def test_weak_entry_keys_are_refused_at_start():
    with pytest.raises(ValueError):
        voice_proxy.create_app("http://127.0.0.1:1", voice_web=True, entry_key="short")


# ------------------------------------------------------------------------------------------------ CSRF
@pytest.mark.parametrize("headers", [
    {}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:1"},
    {"Origin": "SAME", "Sec-Fetch-Site": "cross-site"},
])
def test_csrf_state_changing_posts_need_a_same_origin_origin_header(stack, headers):
    up, _, port = stack
    with browser(port) as c:
        c.headers.pop("Origin")
        h = {k: (f"http://127.0.0.1:{port}" if v == "SAME" else v) for k, v in headers.items()}
        n = len(up.calls)
        r = c.post(url(port, "/api/test/turn"), json={"text": "x"}, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"
        assert len(up.calls) == n


def test_same_site_sec_fetch_and_listed_origins_are_accepted():
    up, clock, srv, prox = make(allowed_origins=("https://demo.example.org",))
    try:
        port = prox.port
        with browser(port) as c:
            ok = c.post(url(port, "/api/test/turn"), json={"text": "x"}, headers={"Origin": "https://demo.example.org", "Sec-Fetch-Site": "same-origin"})
            assert ok.status_code == 200
            bad = c.post(url(port, "/api/test/turn"), json={"text": "x"}, headers={"Origin": "https://demo.example.org.evil.io"})
            assert bad.status_code == 403
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


def test_the_host_header_is_the_default_allowed_origin(stack):
    up, _, port = stack
    with browser(port) as c:
        r = c.post(url(port, "/api/test/turn"), json={"text": "x"}, headers={"Origin": "https://tunnel.example.com", "Host": "tunnel.example.com"})
        assert r.status_code == 200


# ------------------------------------------------------------------------------------------------ expiry
def test_expired_session_is_401_session_expired_and_a_page_reload_recovers(stack):
    up, clock, port = stack
    with browser(port) as c:
        assert c.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code == 200
        clock.t += 1201                                                                         # past the 20 minute lifetime
        r = c.post(url(port, "/api/test/turn"), json={"text": "x"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "session_expired"
        assert c.get(url(port, "/voice/")).status_code == 200                                    # reload starts a new session
        assert c.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code == 200


def test_session_is_renewed_while_in_use_but_capped_by_the_absolute_maximum(stack):
    up, clock, port = stack
    with browser(port) as c:
        started = clock.t
        r = c.post(url(port, "/api/test/turn"), json={"text": "x"})
        assert "set-cookie" not in r.headers                                                     # fresh: nothing to renew
        clock.t += 700                                                                           # more than half of the lifetime used
        r = c.post(url(port, "/api/test/turn"), json={"text": "x"})
        assert "rx_voice=" in r.headers["set-cookie"]
        status, steps = 200, 0
        while status == 200 and steps < 30:                                                      # keeps working for hours while active...
            clock.t += 1100
            steps += 1
            status = c.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code
        assert status == 401 and steps > 5                                                       # ...but never beyond 4 h in total
        assert 14400 <= clock.t - started < 14400 + 1100


def test_expired_session_cannot_fetch_assets_when_an_entry_key_is_required(keyed):
    up, clock, port = keyed
    with browser(port, ENTRY) as c:
        clock.t += 1300
        r = c.get(url(port, "/voice/style.css"))
        assert r.status_code == 401 and r.json()["error"]["code"] == "session_expired"


def test_session_token_unit_behaviour():
    clock = Clock()
    a = voice_proxy.SessionAuth(b"k" * 32, 100, 1000, clock)
    token = a.issue()
    assert a.verify(token)[0] == "ok" and a.verify(None)[0] == "missing" and a.verify("x.y")[0] == "invalid"
    clock.t += 101
    assert a.verify(token)[0] == "expired"
    assert voice_proxy.SessionAuth(b"z" * 32, 100, 1000, clock).verify(a.issue())[0] == "invalid"


# ------------------------------------------------------------------------------------------------ limits
def test_per_session_limit_is_independent_of_other_sessions():
    up, clock, srv, prox = make(per_session_per_min=3, per_ip_per_min=1000)
    try:
        port = prox.port
        with browser(port) as a, browser(port) as b:
            assert [a.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code for _ in range(5)] == [200, 200, 200, 429, 429]
            assert b.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code == 200
            clock.t += 61
            assert a.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code == 200      # the window slides
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


def test_per_ip_limit_covers_all_sessions_of_one_client_ip_and_uses_cf_connecting_ip():
    up, clock, srv, prox = make(per_ip_per_min=4, per_session_per_min=1000)
    try:
        port = prox.port
        with browser(port) as a, browser(port) as b:
            ip1 = {"CF-Connecting-IP": "203.0.113.7"}
            codes = [(a if i % 2 == 0 else b).post(url(port, "/api/test/turn"), json={"text": "x"}, headers=ip1).status_code for i in range(6)]
            assert codes == [200, 200, 200, 200, 429, 429]
            assert a.post(url(port, "/api/test/turn"), json={"text": "x"}, headers={"CF-Connecting-IP": "203.0.113.8"}).status_code == 200
            assert a.post(url(port, "/api/test/turn"), json={"text": "x"}, headers={"CF-Connecting-IP": "not-an-ip; DROP"}).status_code in (200, 429)
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


def test_calls_per_session_are_limited():
    up, clock, srv, prox = make(calls_per_session_10min=2)
    try:
        port = prox.port
        with browser(port) as c:
            assert [c.post(url(port, "/api/voice/calls")).status_code for _ in range(3)] == [201, 201, 429]
            assert c.post(url(port, "/api/voice/calls")).json()["error"]["code"] == "too_many_calls"
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


def test_session_issuance_is_limited_per_ip():
    up, clock, srv, prox = make(issue_per_ip_per_min=3)
    try:
        assert [httpx.get(url(prox.port, "/voice/")).status_code for _ in range(5)] == [200, 200, 200, 429, 429]
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


def test_global_cap_still_applies_across_sessions():
    up, clock, srv, prox = make(rate_per_min=3)
    try:
        port = prox.port
        with browser(port) as c:
            assert [c.post(url(port, "/api/test/turn"), json={"text": "x"}).status_code for _ in range(5)] == [200, 200, 429, 429, 429]   # the page load took one
    finally:
        prox.__exit__(None, None, None)
        srv.__exit__(None, None, None)


# ------------------------------------------------------------------------------------------------ streaming under auth
def test_sse_is_still_streamed_chunk_by_chunk_with_a_session(stack):
    up, _, port = stack
    with browser(port) as c:
        assert c.post(url(port, "/api/voice/calls")).status_code == 201
        with c.stream("POST", url(port, f"/api/voice/calls/{CALL}/turns"), json={"text": "hello"}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            assert r.headers.get("x-accel-buffering") == "no"
            it = r.iter_raw()
            t0 = time.perf_counter()
            first = next(it)
            assert b"event: speech" in first and time.perf_counter() - t0 < 2.0              # arrives while the upstream is still waiting
            assert not up.gate.is_set()
            up.gate.set()
            rest = b"".join(it)
            assert b"event: done" in rest


# ------------------------------------------------------------------------------------------------ health + Agora path
def test_public_health_is_one_word_and_discloses_no_configuration(stack):
    up, _, port = stack
    r = httpx.get(url(port, "/healthz"))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    text = r.text.lower()
    for word in ("brain", "embedder", "llm", "kb_version", "gpt", "agora", "ops", "contract", "key", "secret"):
        assert word not in text


def test_public_health_reports_down_without_details_when_the_runtime_is_unreachable():
    app = voice_proxy.create_app("http://127.0.0.1:9", voice_web=True)
    with Server(app) as p:
        r = httpx.get(url(p.port, "/healthz"))
        assert r.status_code == 503 and r.json() == {"status": "down"}


def test_agora_chat_completions_keeps_bearer_only_auth_and_needs_no_cookie(stack):
    up, _, port = stack
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
    ok = httpx.post(url(port, "/v1/chat/completions"), content=body, headers={"Content-Type": "application/json", "Authorization": GOOD_KEY})
    assert ok.status_code == 200 and up.calls[-1]["headers"]["authorization"] == GOOD_KEY
    n = len(up.calls)
    assert httpx.post(url(port, "/v1/chat/completions"), content=body, headers={"Content-Type": "application/json"}).status_code == 401   # refused locally
    assert len(up.calls) == n
    wrong = httpx.post(url(port, "/v1/chat/completions"), content=body, headers={"Content-Type": "application/json", "Authorization": "Bearer nope"})
    assert wrong.status_code == 401                                                              # the runtime's own bearer check still decides
