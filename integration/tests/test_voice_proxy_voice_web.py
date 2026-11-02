"""voice-web mode of the allow-list proxy: only the page and its API are forwarded; everything else stays 404.
(Authentication, CSRF, expiry, limits and streaming under auth are in test_voice_proxy_auth.py.)"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_proxy  # noqa: E402
from test_voice_proxy import Server  # noqa: E402

CALL = "call-voice-0123456789"


class Upstream:
    def __init__(self):
        self.calls: list[dict] = []
        self.app = Starlette()
        self.app.add_route("/{path:path}", self.any, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])

    async def any(self, request: Request):
        body = await request.body()
        self.calls.append({"method": request.method, "path": request.url.path, "query": dict(request.query_params),
                           "body": body, "headers": {k.lower(): v for k, v in request.headers.items()}})
        if request.url.path.endswith("/turns"):
            async def sse():
                yield b"event: speech\ndata: {}\n\n"
                yield b"event: done\ndata: {}\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream")
        if request.url.path == "/api/voice/calls":
            return JSONResponse({"call_id": CALL}, status_code=201)
        if request.url.path.startswith("/voice"):
            return PlainTextResponse("<html></html>", media_type="text/html")
        return JSONResponse({"ok": True, "status": "ok"})


@pytest.fixture()
def stack():
    up = Upstream()
    with Server(up.app) as u:
        app = voice_proxy.create_app(f"http://127.0.0.1:{u.port}", voice_web=True, rate_per_min=1000, per_ip_per_min=1000, per_session_per_min=1000)
        with Server(app) as p:
            yield up, p.port


def url(port, path):
    return f"http://127.0.0.1:{port}{path}"


@contextlib.contextmanager
def session(port):
    """A browser stand-in: loads the page (gets the session cookie) and sends a same-origin Origin on every request."""
    with httpx.Client(headers={"Origin": f"http://127.0.0.1:{port}"}, timeout=10) as c:
        assert c.get(url(port, "/voice/")).status_code == 200
        yield c


def test_page_files_are_forwarded(stack):
    up, port = stack
    with session(port) as c:
        for path in ("/voice/", "/voice/index.html", "/voice/style.css", "/voice/src/app.js", "/healthz", "/api/voice/config"):
            assert c.get(url(port, path)).status_code == 200, path
    assert up.calls[0]["path"] == "/voice/"


@pytest.mark.parametrize("path", [
    "/voice/README.md", "/voice/package.json", "/voice/mock_server/server.js", "/voice/test/api.test.js", "/voice/src/../README.md", "/voice/%2e%2e/x",
    "/voice/src/a/b.js", "/voice//", "/voice", "/api/sessions/web", "/api/sessions/web/end", "/admin/kb/reload", "/api/voice", "/api/voice/calls/x/turns",
    f"/api/voice/calls/{CALL}/other", f"/api/voice/calls/{CALL}/turns/", "/api/voice/calls/call-voice-0123456789%2f../turns", "/docs", "/openapi.json", "/v1/chat",
])
def test_everything_else_is_404_and_never_reaches_the_upstream(stack, path):
    up, port = stack
    assert httpx.get(url(port, path)).status_code == 404
    assert httpx.post(url(port, path), json={}).status_code == 404
    assert up.calls == []


def test_wrong_methods_are_405(stack):
    up, port = stack
    assert httpx.post(url(port, "/voice/")).status_code == 405
    assert httpx.get(url(port, "/api/voice/calls")).status_code == 405
    assert httpx.delete(url(port, f"/api/voice/calls/{CALL}/end")).status_code == 405
    assert httpx.get(url(port, "/api/test/turn")).status_code == 405
    assert up.calls == []


def test_caller_number_is_stripped_and_other_fields_pass(stack):
    up, port = stack
    with session(port) as c:
        assert c.post(url(port, "/api/test/turn"), json={"text": "hi", "call_id": "c1", "caller_number": "+919999999999"}).status_code == 200
        assert json.loads(up.calls[-1]["body"]) == {"text": "hi", "call_id": "c1"}
        c.post(url(port, "/api/voice/calls"), json={"caller_number": "+919999999999"})
        assert json.loads(up.calls[-1]["body"]) == {}


def test_turn_streams_and_query_is_limited(stack):
    up, port = stack
    with session(port) as c:
        assert c.post(url(port, "/api/voice/calls")).status_code == 201
        with c.stream("POST", url(port, f"/api/voice/calls/{CALL}/turns"), json={"text": "hello"}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            assert b"event: done" in r.read()
        assert c.post(url(port, f"/api/voice/calls/{CALL}/turns?stream=false"), json={"text": "x"}).status_code == 200
        for q in ("?stream=maybe", "?a=1", "?stream=true&stream=false"):
            assert c.post(url(port, f"/api/voice/calls/{CALL}/turns{q}"), json={"text": "x"}).status_code == 400
        assert c.post(url(port, f"/api/voice/calls/{CALL}/cancel?stream=true")).status_code == 400
        assert c.get(url(port, "/voice/?x=1")).status_code == 400


def test_empty_body_allowed_only_where_documented(stack):
    up, port = stack
    with session(port) as c:
        assert c.post(url(port, "/api/voice/calls")).status_code == 201
        assert c.post(url(port, f"/api/voice/calls/{CALL}/end")).status_code == 200
        assert c.post(url(port, f"/api/voice/calls/{CALL}/turns")).status_code == 415       # JSON required
        assert c.post(url(port, f"/api/voice/calls/{CALL}/turns"), content=b"[]", headers={"Content-Type": "application/json"}).status_code == 400
        assert c.post(url(port, "/api/test/turn"), content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415


def test_big_bodies_and_cookies_and_odd_headers(stack):
    up, port = stack
    with session(port) as c:
        assert c.post(url(port, "/api/voice/calls")).status_code == 201
        assert c.post(url(port, f"/api/voice/calls/{CALL}/turns"), json={"text": "x" * 20000}).status_code == 413
        n = len(up.calls)
        c.post(url(port, "/api/test/turn"), json={"text": "a"}, headers={"X-Forwarded-For": "1.2.3.4", "X-Evil": "1", "Authorization": "Bearer t"})
        h = up.calls[n]["headers"]
        assert "cookie" not in h and "x-forwarded-for" not in h and "x-evil" not in h and h["authorization"] == "Bearer t"   # the session cookie never reaches the runtime


def test_global_rate_cap():
    up = Upstream()
    with Server(up.app) as u:
        with Server(voice_proxy.create_app(f"http://127.0.0.1:{u.port}", voice_web=True, rate_per_min=3)) as p:
            codes = [httpx.get(url(p.port, "/api/voice/config")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_default_mode_does_not_serve_voice_routes():
    up = Upstream()
    with Server(up.app) as u:
        with Server(voice_proxy.create_app(f"http://127.0.0.1:{u.port}")) as p:
            assert httpx.get(url(p.port, "/voice/")).status_code == 404
            assert httpx.post(url(p.port, "/api/test/turn"), json={}).status_code == 404
    assert up.calls == []
