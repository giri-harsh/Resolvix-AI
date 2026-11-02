"""Tests for integration/voice_proxy.py: allow-list, header/body controls, streaming, no bypass of the runtime's bearer auth.

Everything runs over real loopback sockets (a fake upstream and the proxy, both on ephemeral 127.0.0.1 ports); nothing touches the
real runtime, ops or any tunnel. Run:  python -m pytest integration/tests -q
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_proxy  # noqa: E402

GOOD_KEY = "Bearer test-shim-key-for-unit-tests"
BODY = json.dumps({"model": "resolvix-brain", "stream": True, "messages": [{"role": "user", "content": "hello"}]})
JSON_H = {"Content-Type": "application/json", "Authorization": GOOD_KEY}


class Server:
    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=5)


class FakeUpstream:
    """Stands in for the voice runtime: checks the bearer like the real shim and records what it received."""

    def __init__(self):
        self.calls: list[dict] = []
        self.gate = threading.Event()          # the SSE stream waits for this after its first chunk
        self.gated = False
        self.redirect_hits = 0
        self.app = Starlette(routes=[])
        self.app.add_route("/v1/chat/completions", self.chat, methods=["POST"])
        self.app.add_route("/redirected", self.redirected, methods=["GET", "POST"])
        self.app.add_route("/{path:path}", self.other, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])

    async def chat(self, request: Request):
        body = await request.body()
        self.calls.append({"headers": {k.lower(): v for k, v in request.headers.items()}, "query": dict(request.query_params), "body": body})
        if request.headers.get("authorization") != GOOD_KEY:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if request.query_params.get("mode") == "redirect":
            return JSONResponse({}, status_code=307, headers={"Location": "http://127.0.0.1:9/redirected"})
        gate, gated = self.gate, self.gated

        async def sse():
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            if gated:
                import asyncio
                for _ in range(100):                  # wait (max 5 s) until the test has received the first chunk
                    if gate.is_set():
                        break
                    await asyncio.sleep(0.05)
            yield b'data: {"choices":[{"delta":{"content":"second"}}]}\n\n'
            yield b"data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")

    async def redirected(self, request: Request):
        self.redirect_hits += 1
        return JSONResponse({})

    async def other(self, request: Request):
        self.calls.append({"other": request.url.path})
        return JSONResponse({"leak": True})


@pytest.fixture()
def stack():
    up = FakeUpstream()
    with Server(up.app) as upsrv:
        up.port = upsrv.port
        app = voice_proxy.create_app(f"http://127.0.0.1:{upsrv.port}")
        with Server(app) as prox:
            yield up, prox.port


def post(port, path="/v1/chat/completions", headers=None, content=BODY, **kw):
    with httpx.Client(timeout=10) as c:
        return c.post(f"http://127.0.0.1:{port}{path}", headers=JSON_H if headers is None else headers, content=content, **kw)


def raw(port: int, request: bytes) -> int:
    """Send exact bytes and return the HTTP status code (so encoded / odd request targets are not normalised by a client library)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(request)
        data = b""
        while b"\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return int(data.split(b" ", 2)[1])


def raw_post(port, target: str, method="POST") -> int:
    body = BODY.encode()
    req = (f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAuthorization: {GOOD_KEY}\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    return raw(port, req)


# ------------------------------------------------------------------------------------------------ forwarding
def test_allowed_request_is_forwarded_with_auth_content_type_and_call_id(stack):
    up, port = stack
    r = post(port, "/v1/chat/completions?call_id=call-web-ab12", headers={**JSON_H, "Cookie": "session=x", "X-Forwarded-For": "1.2.3.4", "X-Evil": "1"})
    assert r.status_code == 200 and b"first" in r.content and b"[DONE]" in r.content
    assert r.headers["content-type"].startswith("text/event-stream")
    got = up.calls[-1]
    assert got["headers"]["authorization"] == GOOD_KEY                       # untouched
    assert got["headers"]["content-type"] == "application/json"
    assert got["query"] == {"call_id": "call-web-ab12"} and got["body"] == BODY.encode()
    for dropped in ("cookie", "x-forwarded-for", "x-evil"):
        assert dropped not in got["headers"]
    assert got["headers"]["accept-encoding"] == "identity"


def test_without_query_is_fine(stack):
    up, port = stack
    assert post(port).status_code == 200 and up.calls[-1]["query"] == {}


def test_the_runtime_bearer_check_is_not_bypassed_or_replaced(stack):
    up, port = stack
    r = post(port, headers={**JSON_H, "Authorization": "Bearer wrong-key"})
    assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"}     # the upstream's own answer, passed through unchanged
    assert len(up.calls) == 1                                                    # it did reach the runtime, which refused it


def test_missing_authorization_is_refused_locally_and_never_forwarded(stack):
    up, port = stack
    r = post(port, headers={"Content-Type": "application/json"})
    assert r.status_code == 401 and up.calls == []
    crlf = "\r\n"
    blank = crlf.join(["POST /v1/chat/completions HTTP/1.1", "Host: x", "Content-Type: application/json", "Authorization:   ",
                       f"Content-Length: {len(BODY)}", "Connection: close", "", BODY]).encode()
    assert raw(port, blank) == 401 and up.calls == []


# ------------------------------------------------------------------------------------------------ streaming
def test_sse_is_streamed_chunk_by_chunk_not_buffered(stack):
    up, port = stack
    up.gated = True                                   # the upstream will not send chunk 2 until the test saw chunk 1
    with httpx.Client(timeout=10) as c:
        with c.stream("POST", f"http://127.0.0.1:{port}/v1/chat/completions", headers=JSON_H, content=BODY) as r:
            assert r.status_code == 200
            it = r.iter_bytes()
            first = b""
            t0 = time.time()
            while b"first" not in first and time.time() - t0 < 4:
                first += next(it)
            assert b"first" in first and b"second" not in first, "first chunk must arrive while the upstream is still holding the rest"
            up.gate.set()
            rest = b"".join(it)
    assert b"second" in rest and b"[DONE]" in rest


# ------------------------------------------------------------------------------------------------ allow-list
@pytest.mark.parametrize("target", [
    "/", "/healthz", "/docs", "/openapi.json", "/redoc", "/test", "/call", "/api/sessions/web", "/api/sessions/web/end", "/api/test/turn", "/api/test/end",
    "/admin/kb/reload", "/v1/chat", "/v1/chat/completions/", "//v1/chat/completions", "///v1/chat/completions", "/v1//chat/completions",
    "/v1/chat/completions%2f", "/v1/chat%2fcompletions", "/v1%2fchat/completions", "/%76%31/chat/completions", "/v1/chat/%63ompletions",
    "/V1/chat/completions", "/v1/Chat/Completions", "/v1/chat/completions;x=1", "/v1/chat/completions%00", "/v1/chat/completions%20",
    "/v1/chat/completions.json", "/v1/chat/completionsX", "/v1/chat/completions/..", "/v1/chat/completions/../../api/sessions/web",
    "/v1/../api/sessions/web", "/%2e%2e/api/sessions/web", "/v1/%2e%2e/api/sessions/web", "/v1/chat/completions/%2e%2e/%2e%2e/admin/kb/reload",
    "/v1/chat/completions/./", "/./v1/chat/completions",
])
def test_every_other_path_is_404_and_never_reaches_the_upstream(stack, target):
    up, port = stack
    status = raw_post(port, target)
    assert status in (404, 400), f"{target!r} -> {status}"
    assert up.calls == [], f"{target!r} reached the upstream"


def test_absolute_form_targets_never_change_the_destination(stack):
    up, port = stack
    for target in ("http://127.0.0.1/api/sessions/web", "http://evil.example/admin/kb/reload", "*"):
        assert raw_post(port, target) in (404, 400)
    assert up.calls == []
    # absolute-form with the allowed path is just that request: the Host in the target is ignored, the destination is the fixed upstream
    assert raw_post(port, "http://evil.example/v1/chat/completions") == 200
    assert up.calls[-1]["headers"]["host"] == f"127.0.0.1:{up.port}"


def test_a_fragment_in_the_request_target_is_dropped_not_forwarded(stack):
    up, port = stack
    assert raw_post(port, "/v1/chat/completions?call_id=x#frag") == 200
    assert up.calls[-1]["query"] == {"call_id": "x"}


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE"])
def test_other_methods_on_the_allowed_path_are_405(stack, method):
    up, port = stack
    with httpx.Client(timeout=5) as c:
        r = c.request(method, f"http://127.0.0.1:{port}/v1/chat/completions", headers=JSON_H, content=BODY if method not in ("GET", "HEAD", "TRACE") else None)
    assert r.status_code == 405 and r.headers.get("allow") == "POST"
    assert up.calls == []


# ------------------------------------------------------------------------------------------------ query, headers, body
@pytest.mark.parametrize("query", ["x=1", "call_id=a&x=1", "call_id=", "call_id=a/b", "call_id=%2e%2e%2fx", "call_id=a&call_id=b", "call_id=" + "a" * 200,
                                   "call_id=a%00b", "call_id=a b", "mode=redirect", "x=1&call_id=a", "call_id[]=a", "CALL_ID=a"])
def test_unexpected_query_strings_are_rejected(stack, query):
    up, port = stack
    r = post(port, f"/v1/chat/completions?{query}")
    assert r.status_code == 400 and up.calls == []


@pytest.mark.parametrize("content,ctype,code", [
    ("not json", "application/json", 400), ("[1,2]", "application/json", 400), ("", "application/json", 400), ("null", "application/json", 400),
    (BODY, "text/plain", 415), (BODY, "application/x-www-form-urlencoded", 415), (BODY, None, 415),
])
def test_malformed_requests_are_rejected_before_the_upstream(stack, content, ctype, code):
    up, port = stack
    headers = {"Authorization": GOOD_KEY, **({"Content-Type": ctype} if ctype else {})}
    r = post(port, headers=headers, content=content)
    assert r.status_code == code and up.calls == []


def test_oversized_declared_and_chunked_bodies_are_413(stack):
    up, port = stack
    big = json.dumps({"stream": True, "messages": [{"role": "user", "content": "x" * (voice_proxy.MAX_BODY_BYTES + 10)}]})
    assert post(port, content=big).status_code == 413                           # Content-Length above the cap
    def gen():                                                                   # no Content-Length: chunked transfer
        for _ in range(40):
            yield b"x" * 10_000
    assert post(port, content=gen()).status_code == 413
    assert up.calls == []


def test_bad_content_length_is_400(stack):
    up, port = stack
    req = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nAuthorization: {GOOD_KEY}\r\n"
           f"Content-Length: 12abc\r\nConnection: close\r\n\r\n{BODY}").encode()
    assert raw(port, req) == 400 and up.calls == []


# ------------------------------------------------------------------------------------------------ failures, redirects, limits
def test_upstream_down_gives_a_generic_502():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead_port = s.getsockname()[1]; s.close()
    with Server(voice_proxy.create_app(f"http://127.0.0.1:{dead_port}")) as prox:
        r = post(prox.port)
    assert r.status_code == 502 and r.json() == {"error": {"code": "upstream_unavailable"}}
    assert "Connect" not in r.text and str(dead_port) not in r.text


def test_upstream_redirects_are_not_followed(stack):
    up, port = stack
    r = post(port, "/v1/chat/completions?call_id=x", headers=JSON_H)  # sanity
    assert r.status_code == 200
    up.calls.clear()
    up_redirect = FakeUpstream()
    # a dedicated upstream that always redirects to a second host: the proxy must hand the 307 back, not follow it
    async def redirecting(request: Request):
        return JSONResponse({}, status_code=307, headers={"Location": "http://127.0.0.1:9/elsewhere"})
    app = Starlette(); app.add_route("/v1/chat/completions", redirecting, methods=["POST"])
    with Server(app) as u2, Server(voice_proxy.create_app(f"http://127.0.0.1:{u2.port}")) as prox:
        r = post(prox.port)
    assert r.status_code == 307 and "location" not in {k.lower() for k in r.headers}


def test_concurrency_cap_returns_503_instead_of_queueing(stack):
    up, _ = stack
    up.gated = True
    with Server(up.app) as upsrv, Server(voice_proxy.create_app(f"http://127.0.0.1:{upsrv.port}", max_concurrent=1)) as prox:
        results = {}
        def slow():
            with httpx.Client(timeout=10) as c, c.stream("POST", f"http://127.0.0.1:{prox.port}/v1/chat/completions", headers=JSON_H, content=BODY) as r:
                results["first"] = r.status_code
                time.sleep(0.8)
                for _ in r.iter_bytes():
                    pass
        t = threading.Thread(target=slow); t.start(); time.sleep(0.4)
        assert post(prox.port).status_code == 503
        up.gate.set(); t.join(timeout=10)
        assert results["first"] == 200
        assert post(prox.port).status_code == 200                                # the slot is free again


# ------------------------------------------------------------------------------------------------ configuration and logging
@pytest.mark.parametrize("bad", ["http://example.com:8000", "http://10.0.0.5:8000", "https://127.0.0.1:8000", "http://127.0.0.1:8000/x",
                                 "http://user:pw@127.0.0.1:8000", "http://127.0.0.1:8000?a=1", "ftp://127.0.0.1", "http://evil.localhost.example"])
def test_upstream_must_be_a_plain_loopback_origin(bad):
    with pytest.raises(ValueError):
        voice_proxy.create_app(bad)


@pytest.mark.parametrize("ok", ["http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:8000/", "http://[::1]:8000"])
def test_loopback_upstreams_are_accepted(ok):
    voice_proxy.create_app(ok)


def test_logs_never_contain_headers_bodies_or_secrets(stack, caplog):
    up, port = stack
    caplog.set_level(logging.DEBUG)
    secret_body = json.dumps({"stream": True, "messages": [{"role": "user", "content": "my-secret-marker-9931"}]})
    post(port, "/v1/chat/completions?call_id=call-1", headers={**JSON_H, "Authorization": "Bearer super-secret-bearer-4471"}, content=secret_body)
    post(port, content="broken-json-marker-5520")
    raw_post(port, "/%2e%2e/etc/passwd")
    proxy_logs = [r for r in caplog.records if r.name == "voice_proxy"]        # the proxy's own log lines (test-client libraries log by themselves)
    assert proxy_logs, "the proxy should log one metadata line per request"
    text = " | ".join(r.getMessage() for r in proxy_logs)
    for marker in ("super-secret-bearer-4471", "Bearer", "my-secret-marker-9931", "broken-json-marker-5520", "authorization", "Authorization", "call-1"):
        assert marker not in text, marker


def test_http_client_loggers_are_silenced_so_urls_with_call_ids_are_not_logged():
    voice_proxy.create_app("http://127.0.0.1:8000")
    assert logging.getLogger("httpx").level >= logging.WARNING and logging.getLogger("httpcore").level >= logging.WARNING
