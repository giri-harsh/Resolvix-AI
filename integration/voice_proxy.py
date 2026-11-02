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

Authentication in voice-web mode (Phase 1.2): every state-changing request (all POST) needs a short-lived signed session cookie that the proxy
itself issues when the page is loaded (``rx_voice``: HttpOnly, SameSite=Strict, Secure behind https, HMAC-SHA256 signed, 20 min, renewed while
in use, 4 h absolute). The browser JavaScript never holds a key; SHIM_API_KEY is never sent to the browser. When VOICE_PROXY_ENTRY_KEY is set, the
cookie is only issued for ``/voice/?k=<entry key>`` (then 303 to ``/voice/``); without it, pages and APIs answer 401. CSRF: cookie is SameSite=Strict
and every POST must carry an ``Origin`` that equals the request Host (or VOICE_PROXY_ALLOWED_ORIGINS) and must not be ``Sec-Fetch-Site: cross-site``.
Calls created through /api/voice/calls are bound to the session that created them. Limits per client IP and per session apply on top of the
global cap. ``GET /healthz`` is reduced to ``{"status": ...}``. The Agora path /v1/chat/completions is untouched: it keeps its own bearer check
in the runtime and needs no cookie.

Run (local only):  python integration/voice_proxy.py        # listens on 127.0.0.1:8800
Env: VOICE_PROXY_PORT (8800), VOICE_PROXY_UPSTREAM (http://127.0.0.1:8000, must be loopback)
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import deque
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

SESSION_COOKIE = "rx_voice"
ENTRY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionAuth:
    """Stateless signed session tokens: payload.signature, payload = {s: session id, b: born, e: expires}."""

    def __init__(self, secret: bytes, ttl_s: float, abs_max_s: float, clock=time.time) -> None:
        self._secret, self.ttl_s, self.abs_max_s, self.clock = secret, ttl_s, abs_max_s, clock

    def _sign(self, payload_b64: str) -> str:
        return _b64(hmac.new(self._secret, payload_b64.encode(), hashlib.sha256).digest())

    def issue(self, sid: str | None = None, born: float | None = None) -> str:
        now = self.clock()
        born = now if born is None else born
        exp = min(now + self.ttl_s, born + self.abs_max_s)
        payload = _b64(json.dumps({"s": sid or secrets.token_urlsafe(12), "b": born, "e": exp}, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str | None) -> tuple[str, dict | None]:
        """-> ("ok" | "missing" | "invalid" | "expired", payload)"""
        if not token:
            return "missing", None
        try:
            payload_b64, sig = token.split(".", 1)
            if not hmac.compare_digest(sig, self._sign(payload_b64)):
                return "invalid", None
            data = json.loads(_unb64(payload_b64))
            sid, born, exp = str(data["s"]), float(data["b"]), float(data["e"])
        except Exception:
            return "invalid", None
        now = self.clock()
        if now >= exp or now >= born + self.abs_max_s:
            return "expired", None
        return "ok", {"s": sid, "b": born, "e": exp}

    def renewal(self, payload: dict) -> str | None:
        """A fresh token when less than half the lifetime is left (sliding window, capped by the absolute maximum)."""
        if payload["e"] - self.clock() < self.ttl_s / 2 and self.clock() < payload["b"] + self.abs_max_s:
            return self.issue(payload["s"], payload["b"])
        return None


class Window:
    """Sliding-window counter per key: allows ``limit`` hits per ``seconds``."""

    def __init__(self, limit: int, seconds: float, clock=time.time) -> None:
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self._hits: dict[str, deque] = {}

    def hit(self, key: str) -> bool:
        now = self.clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.seconds:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > 5000:                      # bounded memory: drop idle keys
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.seconds][:1000]:
                self._hits.pop(k, None)
        return True


def _client_ip(request: Request) -> str:
    """Behind cloudflared the peer is loopback and Cloudflare puts the visitor in CF-Connecting-IP; only local processes can reach this port."""
    candidate = request.headers.get("cf-connecting-ip", "").strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return request.client.host if request.client else "unknown"


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
               total_timeout_s: float = TOTAL_TIMEOUT_S, voice_web: bool = False, rate_per_min: int = 120,
               entry_key: str | None = None, session_secret: bytes | None = None, allowed_origins: tuple[str, ...] = (),
               session_ttl_s: float = 1200.0, session_abs_max_s: float = 14400.0, per_session_per_min: int = 60,
               per_ip_per_min: int = 90, calls_per_session_10min: int = 6, issue_per_ip_per_min: int = 10,
               failures_per_ip_per_min: int = 10, clock=time.time) -> Starlette:
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

    if entry_key is not None and not ENTRY_KEY_RE.fullmatch(entry_key):
        raise ValueError("VOICE_PROXY_ENTRY_KEY must be 16-128 characters of A-Z a-z 0-9 _ -")
    auth = SessionAuth(session_secret or secrets.token_bytes(32), session_ttl_s, session_abs_max_s, clock)
    ip_window = Window(per_ip_per_min, 60.0, clock)
    static_window = Window(60, 60.0, clock)
    session_window = Window(per_session_per_min, 60.0, clock)
    calls_window = Window(calls_per_session_10min, 600.0, clock)
    issue_window = Window(issue_per_ip_per_min, 60.0, clock)
    fail_window = Window(failures_per_ip_per_min, 60.0, clock)
    global_window = Window(rate_per_min, 60.0, clock)
    session_calls: dict[str, set[str]] = {}          # session id -> call ids it created through /api/voice/calls
    origins_ok = {o.rstrip("/").lower() for o in allowed_origins}

    def rate_limited() -> bool:
        return not global_window.hit("all")

    def secure_request(request: Request) -> bool:
        return request.headers.get("x-forwarded-proto", "").lower() == "https" or '"https"' in request.headers.get("cf-visitor", "")

    def cookie_header(token: str, request: Request) -> str:
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(session_ttl_s)}" + ("; Secure" if secure_request(request) else "")

    def origin_ok(request: Request) -> bool:
        origin = request.headers.get("origin", "").strip().rstrip("/").lower()
        if not origin or origin == "null":
            return False
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return False
        return origin in origins_ok or urlsplit(origin).netloc == request.headers.get("host", "").strip().lower()

    def with_cookie(resp: Response, token: str | None, request: Request) -> Response:
        if token:
            resp.headers.append("set-cookie", cookie_header(token, request))
        return resp

    async def healthz_minimal() -> Response:
        """Public health: one word. The runtime's own /healthz (brain, KB, model, ops and Agora details) never leaves the machine."""
        try:
            resp = await client.get(f"{base}/healthz", timeout=2.5)
            status = resp.json().get("status") if resp.status_code == 200 else None
        except Exception:
            status = None
        status = status if status in ("ok", "degraded") else "down"
        return JSONResponse({"status": status}, status_code=200 if status != "down" else 503, headers={"Cache-Control": "no-store"})

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

        # query: only ?k=<entry key> on the page itself, only ?stream=true|false on turns, nothing elsewhere
        qs = request.scope.get("query_string", b"")
        params = None
        entry_try = None
        if qs:
            items = request.query_params.multi_items()
            if raw_path == b"/voice/" and len(items) == 1 and items[0][0] == "k" and ENTRY_KEY_RE.fullmatch(items[0][1]):
                entry_try = items[0][1]
            elif raw_path.endswith(b"/turns") and len(items) == 1 and items[0][0] == "stream" and items[0][1] in ("true", "false"):
                params = {"stream": items[0][1]}
            else:
                return _error(400, "bad_query")

        ip = _client_ip(request)
        cookie = request.cookies.get(SESSION_COOKIE)
        state, sess = auth.verify(cookie)
        renew = auth.renewal(sess) if sess else None

        if route == "get" and raw_path == b"/healthz":
            if not static_window.hit(ip):
                return _error(429, "rate_limited", {"Retry-After": "5"})
            return await healthz_minimal()

        # ---- the page and its assets: this is where a session is granted
        if route == "static":
            if not static_window.hit(ip):
                return _error(429, "rate_limited", {"Retry-After": "5"})
            if entry_try is not None:
                if entry_key is None or hmac.compare_digest(entry_try.encode(), entry_key.encode()):
                    if not issue_window.hit(ip):
                        return _error(429, "rate_limited", {"Retry-After": "10"})
                    return with_cookie(Response(status_code=303, headers={"Location": "/voice/", "Cache-Control": "no-store"}), auth.issue(), request)
                if not fail_window.hit(ip):
                    return _error(429, "rate_limited", {"Retry-After": "30"})
                return _error(403, "access_denied")
            if state != "ok":
                if entry_key is not None:
                    return _error(401, "session_expired" if state == "expired" else "session_required")
                if not issue_window.hit(ip):
                    return _error(429, "rate_limited", {"Retry-After": "10"})
                renew = auth.issue()                       # open mode (no entry key): the page load itself starts the session
        else:
            # ---- everything that changes state (all POST) needs a valid session, a same-origin Origin and is limited per IP and per session
            if request.method == "POST":
                if state != "ok":
                    return _error(401, "session_expired" if state == "expired" else "unauthenticated")
                if not origin_ok(request):
                    return _error(403, "bad_origin")
                if not ip_window.hit(ip):
                    return _error(429, "rate_limited", {"Retry-After": "5"})
                if not session_window.hit(sess["s"]):
                    return _error(429, "rate_limited", {"Retry-After": "5"})
                if raw_path == b"/api/voice/calls" and not calls_window.hit(sess["s"]):
                    return _error(429, "too_many_calls", {"Retry-After": "30"})
                if raw_path.startswith(b"/api/voice/calls/"):
                    call = raw_path.split(b"/")[4].decode("ascii")
                    if call not in session_calls.get(sess["s"], ()):
                        return _error(403, "not_your_call")
            elif not ip_window.hit(ip):
                return _error(429, "rate_limited", {"Retry-After": "5"})

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

        out = {"x-accel-buffering": "no"}
        out.update({k: v for k, v in upstream_resp.headers.items() if k.lower() in VOICE_RESPONSE_HEADERS})
        out.setdefault("cache-control", "no-cache")

        if raw_path == b"/api/voice/calls":               # small JSON: read it whole so the new call can be bound to this session
            try:
                data = await upstream_resp.aread()
            except Exception:
                data = b""
            finally:
                await upstream_resp.aclose()
                release()
            if upstream_resp.status_code == 201:
                try:
                    session_calls.setdefault(sess["s"], set()).add(str(json.loads(data)["call_id"]))
                except Exception:
                    pass
            return with_cookie(Response(data, status_code=upstream_resp.status_code, headers=out), renew, request)

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

        return with_cookie(StreamingResponse(body_iter(), status_code=upstream_resp.status_code, media_type=None, headers=out), renew, request)

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
    env = os.environ.get
    secret = env("VOICE_PROXY_SESSION_SECRET", "").encode() or None          # unset: a random per-process secret (sessions end on restart)
    app = create_app(env("VOICE_PROXY_UPSTREAM", "http://127.0.0.1:8000"), voice_web=voice_web,
                     rate_per_min=int(env("VOICE_PROXY_RATE_PER_MIN", "120")),
                     entry_key=env("VOICE_PROXY_ENTRY_KEY", "").strip() or None, session_secret=secret,
                     allowed_origins=tuple(o for o in env("VOICE_PROXY_ALLOWED_ORIGINS", "").split(",") if o.strip()),
                     session_ttl_s=float(env("VOICE_PROXY_SESSION_TTL_S", "1200")),
                     per_session_per_min=int(env("VOICE_PROXY_PER_SESSION_PER_MIN", "60")),
                     per_ip_per_min=int(env("VOICE_PROXY_PER_IP_PER_MIN", "90")))
    log.info("voice proxy on 127.0.0.1:%d -> POST /v1/chat/completions%s only (upstream %s)", port, " + voice-web routes" if voice_web else "",
             _loopback_upstream(os.environ.get("VOICE_PROXY_UPSTREAM", "http://127.0.0.1:8000")))
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning", timeout_keep_alive=5,
                limit_concurrency=100, h11_max_incomplete_event_size=16 * 1024)


if __name__ == "__main__":
    main()
