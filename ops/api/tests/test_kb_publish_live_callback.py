"""C-1: dashboard Publish in RUNTIME_MODE=live must not deadlock when the runtime calls GET /v1/kb/export back.

Real uvicorn for ops (file database, one worker) and a tiny fake voice runtime whose /admin/kb/reload handler does what
the real one does: it fetches the export URL it was given, with X-Service-Key, and answers with a KBVersionInfo.
Before the fix the publish request held a database write lock while it waited for the runtime, so the export callback
failed with "database is locked" (HTTP 500 after 5 s) and the publish ended as 422/502.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import uvicorn

from _m2 import load_fixture
from resolvix_ops.app import create_app

SERVICE_KEY = "test-service-key-12345"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeRuntime:
    """Stands in for voice-runtime: POST /admin/kb/reload -> GET {location} with X-Service-Key -> KBVersionInfo."""

    def __init__(self):
        self.port = free_port()
        self.callbacks = []  # (status, seconds) of each export callback
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if self.path != "/admin/kb/reload" or self.headers.get("X-Service-Key") != SERVICE_KEY:
                    self.send_response(401)
                    self.end_headers()
                    return
                t0 = time.monotonic()
                r = httpx.get(body["location"], headers={"X-Service-Key": SERVICE_KEY}, timeout=15)
                outer.callbacks.append((r.status_code, round(time.monotonic() - t0, 2)))
                if r.status_code != 200:
                    payload, status = {"error": {"code": "validation_error",
                                                 "message": f"KB reload compilation failed: cannot fetch export: HTTP {r.status_code}",
                                                 "retryable": False, "details": {}}}, 422
                else:
                    payload, status = load_fixture("kb_version_info"), 200
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def start_ops(test_settings, runtime_port: int):
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit_event = None
    port = free_port()
    settings = test_settings.model_copy(update=dict(
        seed_users_on_startup=True, runtime_mode="live", runtime_url=f"http://127.0.0.1:{runtime_port}",
        ops_public_url=f"http://127.0.0.1:{port}", session_cookie_secure=False, kb_publish_timeout_s=20))
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    return app, server, thread, f"http://127.0.0.1:{port}", settings


def stop_ops(app, server, thread):
    server.should_exit = True
    thread.join(timeout=10)
    app.state.engine.dispose()


def admin_client(base_url: str, password: str) -> httpx.Client:
    c = httpx.Client(base_url=base_url, timeout=30)
    assert c.post("/v1/auth/login", json={"email": "admin@demo", "password": password}).status_code == 200
    return c


def test_dashboard_publish_in_live_mode_survives_the_runtime_calling_back(test_settings):
    with FakeRuntime() as runtime:
        app, server, thread, base, settings = start_ops(test_settings, runtime.port)
        try:
            admin = admin_client(base, settings.seed_password)
            t0 = time.monotonic()
            r = admin.post("/v1/kb/publish")
            took = time.monotonic() - t0
            assert r.status_code == 200, (r.status_code, r.text[:300], runtime.callbacks)
            assert r.json()["kb_version"] == load_fixture("kb_version_info")["kb_version"]
            assert took < 5, f"publish took {took:.1f}s"
            assert runtime.callbacks and runtime.callbacks[0][0] == 200 and runtime.callbacks[0][1] < 2
            hist = admin.get("/v1/kb/publishes").json()["items"]
            assert len(hist) == 1 and hist[0]["mode"] == "live"
        finally:
            stop_ops(app, server, thread)


def test_export_callback_also_works_while_a_staff_session_publishes_nothing_else_is_blocked(test_settings):
    """While a publish is waiting for the runtime, other requests (a ticket, the export itself) must not block."""
    with FakeRuntime() as runtime:
        app, server, thread, base, settings = start_ops(test_settings, runtime.port)
        try:
            admin = admin_client(base, settings.seed_password)
            service = httpx.Client(base_url=base, headers={"X-Service-Key": SERVICE_KEY}, timeout=30)
            result = {}

            def publish():
                result["r"] = admin.post("/v1/kb/publish")

            t = threading.Thread(target=publish)
            t.start()
            for i in range(10):  # traffic during the publish
                body = load_fixture("ticket_create")
                body["idempotency_key"] = f"during-publish-{i}"
                t0 = time.monotonic()
                assert service.post("/v1/tickets", json=body).status_code == 201
                assert time.monotonic() - t0 < 2
                assert service.get("/v1/kb/export").status_code == 200
            t.join(timeout=30)
            assert result["r"].status_code == 200
        finally:
            stop_ops(app, server, thread)


def test_read_sessions_use_a_deferred_begin_and_write_sessions_begin_immediate(client):
    from sqlalchemy import event, text
    seen = []
    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: seen.append(stmt.strip().upper()))
    with client.app.state.read_session_factory() as db:
        db.execute(text("select 1"))
    reads = list(seen)
    seen.clear()
    with client.app.state.session_factory() as db:
        db.execute(text("select 1"))
    assert "BEGIN" in reads and not any(s.startswith("BEGIN IMMEDIATE") for s in reads)
    assert any(s.startswith("BEGIN IMMEDIATE") for s in seen)


def test_a_reader_is_not_blocked_by_a_write_transaction_that_stays_open(client, auth):
    """While some request holds the write lock, plain GET lists still answer (they read a WAL snapshot)."""
    from sqlalchemy import text
    holder = client.app.state.session_factory()
    holder.execute(text("select 1"))  # BEGIN IMMEDIATE: the write lock is now held
    try:
        t0 = time.monotonic()
        assert client.get("/v1/tickets", headers=auth).status_code == 200
        assert client.get("/v1/kb/export", headers=auth).status_code == 200
        assert time.monotonic() - t0 < 2
    finally:
        holder.rollback()
        holder.close()
