"""Allow-list reverse proxy in front of the voice runtime for an external (tunnel) voice test.

Only ONE request is ever forwarded:  POST /v1/chat/completions[?call_id=<id>]  ->  <upstream>/v1/chat/completions
Everything else is answered locally with 404 (unknown path) or 405 (known path, wrong method) and never reaches the runtime.
It is the only thing a public tunnel should point at; the Agora cloud calls exactly this URL (custom-LLM URL).

What it does NOT do: it does not authenticate. The runtime still checks ``Authorization: Bearer <SHIM_API_KEY>`` and answers 401
itself; the proxy forwards the header untouched and never learns the key. A request without any Authorization header is refused
here (401) only to save a hop.

Controls: exact raw-path match (no decoding, no normalisation, no trailing slash), query limited to a single safe ``call_id``,
Content-Type must be application/json, bounded body (default 256 KiB, also for chunked bodies), JSON object required, header
allow-list (Authorization, Content-Type, Accept only; cookies, Host, X-Forwarded-* are dropped), fixed loopback upstream,
connect/read/total timeouts, concurrency cap, streaming (SSE) passed through chunk by chunk, upstream errors reduced to a generic
502, no logging of headers, bodies, query strings or secrets.

Run (local only):  python integration/voice_proxy.py        # listens on 127.0.0.1:8800
Env: VOICE_PROXY_PORT (8800), VOICE_PROXY_UPSTREAM (http://127.0.0.1:8000, must be loopback)
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger("voice_proxy")

ALLOWED_PATH = b"/v1/chat/completions"
ALLOWED_METHOD = "POST"
MAX_BODY_BYTES = 256 * 1024
MAX_CONCURRENT = 20
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 30.0          # max silence between two streamed chunks
TOTAL_TIMEOUT_S = 120.0        # hard cap for one request
FORWARD_REQUEST_HEADERS = ("authorization", "content-type", "accept")
CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _loopback_upstream(url: str) -> str:
    """The upstream is fixed at start-up and must be a loopback http URL: the proxy can never be pointed at another host."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if parts.scheme != "http" or not is_loopback or parts.path not in ("", "/") or parts.query or parts.username:
        raise ValueError("VOICE_PROXY_UPSTREAM must be a plain http loopback origin such as http://127.0.0.1:8000")
    return f"http://{host}:{parts.port or 80}"


def _error(status: int, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status, headers=headers)


def create_app(upstream: str = "http://127.0.0.1:8000", transport: httpx.AsyncBaseTransport | None = None,
               max_body_bytes: int = MAX_BODY_BYTES, max_concurrent: int = MAX_CONCURRENT,
               total_timeout_s: float = TOTAL_TIMEOUT_S) -> Starlette:
    base = _loopback_upstream(upstream)
    for noisy in ("httpx", "httpcore"):        # these log full request URLs (including ?call_id=...) at INFO/DEBUG: keep them out of the logs
        logging.getLogger(noisy).setLevel(logging.WARNING)
    target = f"{base}/v1/chat/completions"
    limit = asyncio.Semaphore(max_concurrent)
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=10.0, pool=2.0),
        follow_redirects=False,      # never follow an upstream redirect to somewhere else
    )

    async def handler(request: Request) -> Response:
        raw_path: bytes = request.scope.get("raw_path") or request.scope["path"].encode()
        # 1. exact path only: compare the undecoded bytes, so %2e, %2f, //, trailing slash, ;params and case tricks never match
        if raw_path != ALLOWED_PATH:
            return _error(404, "not_found")
        if request.method != ALLOWED_METHOD:
            return _error(405, "method_not_allowed", {"Allow": ALLOWED_METHOD})

        # 2. query: nothing, or exactly one safe call_id
        qs = request.scope.get("query_string", b"")
        call_id = None
        if qs:
            params = request.query_params.multi_items()
            if len(params) != 1 or params[0][0] != "call_id" or not CALL_ID_RE.fullmatch(params[0][1]):
                return _error(400, "bad_query")
            call_id = params[0][1]

        # 3. headers
        ctype = request.headers.get("content-type", "")
        if ctype.split(";")[0].strip().lower() != "application/json":
            return _error(415, "unsupported_media_type")
        if not request.headers.get("authorization", "").strip():
            return _error(401, "missing_authorization")           # the key itself is checked by the runtime, not here
        cl = request.headers.get("content-length")
        if cl is not None:
            if not cl.isdigit():
                return _error(400, "bad_content_length")
            if int(cl) > max_body_bytes:
                return _error(413, "body_too_large")

        # 4. bounded body (also protects against chunked uploads without Content-Length)
        chunks, size = [], 0
        try:
            async for part in request.stream():
                size += len(part)
                if size > max_body_bytes:
                    return _error(413, "body_too_large")
                chunks.append(part)
        except Exception:
            return _error(400, "bad_body")
        body = b"".join(chunks)
        try:
            if not isinstance(json.loads(body), dict):
                raise ValueError
        except Exception:
            return _error(400, "bad_json")

        if limit.locked():
            return _error(503, "busy", {"Retry-After": "2"})

        # 5. forward: fixed target, allow-listed headers only
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        fwd_headers["accept-encoding"] = "identity"     # the stream is passed through raw; never let the upstream compress it
        params = {"call_id": call_id} if call_id else None
        rid, t0 = uuid.uuid4().hex[:8], time.perf_counter()
        await limit.acquire()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                limit.release()

        try:
            req = client.build_request("POST", target, headers=fwd_headers, params=params, content=body)
            upstream_resp = await asyncio.wait_for(client.send(req, stream=True), timeout=total_timeout_s)
        except Exception as exc:                                  # connect/timeout/anything: generic, no details
            release()
            log.warning("proxy %s upstream_error %s", rid, type(exc).__name__)
            return _error(502, "upstream_unavailable")

        passthrough = {"cache-control": "no-cache", "x-accel-buffering": "no"}

        async def body_iter():
            try:
                async with asyncio.timeout(total_timeout_s - (time.perf_counter() - t0)):
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
            except Exception as exc:
                log.warning("proxy %s stream_aborted %s", rid, type(exc).__name__)
            finally:
                await upstream_resp.aclose()
                release()
                log.info("proxy %s POST /v1/chat/completions -> %s in %d ms", rid, upstream_resp.status_code, (time.perf_counter() - t0) * 1000)

        out_headers = dict(passthrough)
        ct = upstream_resp.headers.get("content-type")
        return StreamingResponse(body_iter(), status_code=upstream_resp.status_code, media_type=None,
                                 headers={**out_headers, **({"content-type": ct} if ct else {})})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    app = Starlette(debug=False, lifespan=lifespan)
    # one catch-all route: every method and path reaches the handler, which decides (404 / 405 / forward)
    app.add_route("/{path:path}", handler, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT"])
    app.add_route("/", handler, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    port = int(os.environ.get("VOICE_PROXY_PORT", "8800"))
    app = create_app(os.environ.get("VOICE_PROXY_UPSTREAM", "http://127.0.0.1:8000"))
    log.info("voice proxy on 127.0.0.1:%d -> only POST /v1/chat/completions (upstream %s)", port,
             _loopback_upstream(os.environ.get("VOICE_PROXY_UPSTREAM", "http://127.0.0.1:8000")))
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning", timeout_keep_alive=5,
                limit_concurrency=100, h11_max_incomplete_event_size=16 * 1024)


if __name__ == "__main__":
    main()
