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

Optional voice-web mode (create_app(voice_web=True) / VOICE_PROXY_VOICE_WEB=1): additionally forwards the browser voice page and its API,
and nothing else: GET /voice/, /voice/index.html, /voice/style.css, /voice/src/<file>.js, GET /healthz, POST /api/test/turn, POST /api/test/end,
GET /api/voice/config, POST /api/voice/calls and POST /api/voice/calls/<call-voice-id>/{turns,cancel,end}. Those requests get: exact path match
by regex on the raw path, a 16 KiB body cap, a JSON object body (empty allowed for calls/cancel/end), only ?stream=true|false on turns, the
``caller_number`` field removed from request bodies (a public caller can never make the runtime text an arbitrary number), and a global
request-rate cap (VOICE_PROXY_RATE_PER_MIN, default 120) on top of the concurrency cap. Cookies, Host, X-Forwarded-* and every unlisted header are dropped.

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

# voice-web mode: (method, regex on the raw path, kind). kind: static | get | json (JSON object body required) | optjson (body may be empty)
VOICE_CALL = rb"call-voice-[0-9a-f]{10}"
VOICE_ROUTES = (
    ("GET", re.compile(rb"^/voice/(?:index\.html|style\.css|src/[A-Za-z0-9_-]{1,40}\.js)?$"), "static"),
    ("GET", re.compile(rb"^/healthz$"), "get"),
    ("GET", re.compile(rb"^/api/voice/config$"), "get"),
    ("POST", re.compile(rb"^/api/test/(?:turn|end)$"), "json"),
    ("POST", re.compile(rb"^/api/voice/calls$"), "optjson"),
    ("POST", re.compile(rb"^/api/voice/calls/" + VOICE_CALL + rb"/turns$"), "json"),
    ("POST", re.compile(rb"^/api/voice/calls/" + VOICE_CALL + rb"/(?:cancel|end)$"), "optjson"),
)
VOICE_MAX_BODY = 16 * 1024
VOICE_RESPONSE_HEADERS = ("content-type", "cache-control")
STRIP_FIELDS = ("caller_number",)


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
               total_timeout_s: float = TOTAL_TIMEOUT_S, voice_web: bool = False, rate_per_min: int = 120) -> Starlette:
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

    stamps: list[float] = []

    def rate_limited() -> bool:
        now = time.monotonic()
        while stamps and now - stamps[0] > 60.0:
            stamps.pop(0)
        if len(stamps) >= rate_per_min:
            return True
        stamps.append(now)
        return False

    async def voice_handler(request: Request, raw_path: bytes) -> Response:
        """voice-web mode: forward one of the fixed routes in VOICE_ROUTES to the same fixed upstream, nothing else."""
        route = None
        path_known = False
        for method, rx, kind in VOICE_ROUTES:
            if rx.fullmatch(raw_path):
                path_known = True
                if request.method == method:
                    route = kind
                    break
        if route is None:
            return _error(405 if path_known else 404, "method_not_allowed" if path_known else "not_found")
        qs = request.scope.get("query_string", b"")
        params = None
        if qs:
            items = request.query_params.multi_items()
            if not raw_path.endswith(b"/turns") or len(items) != 1 or items[0][0] != "stream" or items[0][1] not in ("true", "false"):
                return _error(400, "bad_query")
            params = {"stream": items[0][1]}
        body = b""
        if request.method == "POST":
            cl = request.headers.get("content-length")
            if cl is not None and not cl.isdigit():
                return _error(400, "bad_content_length")
            if cl is not None and int(cl) > VOICE_MAX_BODY:
                return _error(413, "body_too_large")
            size, chunks = 0, []
            try:
                async for part in request.stream():
                    size += len(part)
                    if size > VOICE_MAX_BODY:
                        return _error(413, "body_too_large")
                    chunks.append(part)
            except Exception:
                return _error(400, "bad_body")
            body = b"".join(chunks)
            if body.strip() or route == "json":
                if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                    return _error(415, "unsupported_media_type")
                try:
                    obj = json.loads(body)
                    if not isinstance(obj, dict):
                        raise ValueError
                except Exception:
                    return _error(400, "bad_json")
                for field in STRIP_FIELDS:
                    obj.pop(field, None)
                body = json.dumps(obj, ensure_ascii=False).encode()
            else:
                body = b""
        if rate_limited():
            return _error(429, "rate_limited", {"Retry-After": "5"})
        if limit.locked():
            return _error(503, "busy", {"Retry-After": "2"})
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        fwd_headers["accept-encoding"] = "identity"
        rid, t0 = uuid.uuid4().hex[:8], time.perf_counter()
        await limit.acquire()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                limit.release()

        try:
            req = client.build_request(request.method, f"{base}{raw_path.decode('ascii')}", headers=fwd_headers, params=params,
                                       content=body if request.method == "POST" and body else None)
            upstream_resp = await asyncio.wait_for(client.send(req, stream=True), timeout=total_timeout_s)
        except Exception as exc:
            release()
            log.warning("proxy %s voice upstream_error %s", rid, type(exc).__name__)
            return _error(502, "upstream_unavailable")

        async def body_iter():
            try:
                async with asyncio.timeout(total_timeout_s - (time.perf_counter() - t0)):
                    async for chunk in upstream_resp.aiter_raw():
                        yield chunk
            except Exception as exc:
                log.warning("proxy %s voice stream_aborted %s", rid, type(exc).__name__)
            finally:
                await upstream_resp.aclose()
                release()
                log.info("proxy %s voice %s -> %s in %d ms", rid, route, upstream_resp.status_code, (time.perf_counter() - t0) * 1000)

        out = {"x-accel-buffering": "no"}
        out.update({k: v for k, v in upstream_resp.headers.items() if k.lower() in VOICE_RESPONSE_HEADERS})
        out.setdefault("cache-control", "no-cache")
        return StreamingResponse(body_iter(), status_code=upstream_resp.status_code, media_type=None, headers=out)

    async def handler(request: Request) -> Response:
        raw_path: bytes = request.scope.get("raw_path") or request.scope["path"].encode()
        if voice_web and raw_path != ALLOWED_PATH:
            return await voice_handler(request, raw_path)
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
    voice_web = os.environ.get("VOICE_PROXY_VOICE_WEB", "0") in ("1", "true", "yes")
    app = create_app(os.environ.get("VOICE_PROXY_UPSTREAM", "http://127.0.0.1:8000"), voice_web=voice_web,
                     rate_per_min=int(os.environ.get("VOICE_PROXY_RATE_PER_MIN", "120")))
    log.info("voice proxy on 127.0.0.1:%d -> POST /v1/chat/completions%s only (upstream %s)", port, " + voice-web routes" if voice_web else "",
             _loopback_upstream(os.environ.get("VOICE_PROXY_UPSTREAM", "http://127.0.0.1:8000")))
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning", timeout_keep_alive=5,
                limit_concurrency=100, h11_max_incomplete_event_size=16 * 1024)


if __name__ == "__main__":
    main()
