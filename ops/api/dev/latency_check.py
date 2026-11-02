"""Latency of the endpoints the voice runtime calls during a live call (file database, real uvicorn).

Run with: python -X utf8 dev/latency_check.py [--n 200] [--limit-ms 50]
Starts its own uvicorn on a throwaway database (port from SMOKE_PORT, default 8100, or a free one), sends N sequential
requests per endpoint over one keep-alive connection, prints p50/p95/p99/max and exits 1 when any p95 exceeds the limit.
"""
import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

OPS_API_DIR = Path(__file__).resolve().parent.parent
FIXTURES = OPS_API_DIR.parent.parent / "contracts" / "fixtures" / "ops"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def free_port() -> int:
    wanted = int(os.environ.get("SMOKE_PORT", "8100"))
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", wanted)) != 0:
            return wanted
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--limit-ms", type=float, default=50.0)
    args = ap.parse_args()

    env_vars = {}
    for line in (OPS_API_DIR / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip()
    key = env_vars["SERVICE_KEY"]
    port = free_port()
    db_name = f"latency_{uuid.uuid4().hex[:8]}.db"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///./data/{db_name}", NOTIFY_MODE="mock", PORT=str(port))
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "uvicorn", "resolvix_ops.app:app", "--port", str(port),
                             "--loop", "asyncio", "--workers", "1"], cwd=OPS_API_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results: dict[str, list[float]] = {}
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-Service-Key": key}, timeout=10.0)
        for _ in range(80):
            try:
                if client.get("/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.25)
        run = uuid.uuid4().hex[:6]
        call_id = f"lat-{run}"
        cs = fixture("call_start")
        cs["call_id"] = call_id
        assert client.post("/v1/calls", json=cs).status_code == 201

        def timed(label: str, method: str, url: str, body, ok: tuple[int, ...]) -> None:
            t0 = time.perf_counter()
            r = client.request(method, url, json=body)
            ms = (time.perf_counter() - t0) * 1000
            if r.status_code not in ok:
                raise SystemExit(f"{label}: unexpected {r.status_code}")
            results.setdefault(label, []).append(ms)

        turn = fixture("turn_summary")
        for i in range(args.n):
            body = dict(turn, call_id=call_id, turn_index=i)
            timed("POST /v1/calls/{id}/turns", "POST", f"/v1/calls/{call_id}/turns", body, (201,))
        for i in range(args.n):
            t = fixture("ticket_create")
            t.update(idempotency_key=f"lat-{run}-{i}", call_id=call_id, priority="P2", department="exam_cell")
            timed("POST /v1/tickets", "POST", "/v1/tickets", t, (201,))
        for i in range(args.n):
            sms = fixture("sms_request")
            sms.update(idempotency_key=f"lat-sms-{run}-{i}", call_id=call_id, ticket_id=None)
            timed("POST /v1/notifications/sms (mock)", "POST", "/v1/notifications/sms", sms, (202,))
        for i in range(args.n):
            gap = {"question": f"bus route question number {i} timing", "lang": "hinglish", "department_guess": "transport",
                   "call_id": call_id}
            timed("POST /v1/gaps", "POST", "/v1/gaps", gap, (201,))
        for i in range(args.n):
            timed("POST ticket event", "POST", "/v1/tickets/EX-1001/events",
                  dict(fixture("ticket_event"), at=fixture("ticket_event")["at"]), (201, 404))
        client.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        for suffix in ("", "-wal", "-shm"):
            for _ in range(20):
                try:
                    (OPS_API_DIR / "data" / f"{db_name}{suffix}").unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.25)

    print(f"{'endpoint':38} {'n':>4} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}  (ms)")
    worst = 0.0
    for label, values in results.items():
        p95 = pct(values, 95)
        worst = max(worst, p95)
        print(f"{label:38} {len(values):4d} {statistics.median(values):7.1f} {p95:7.1f} {pct(values, 99):7.1f} {max(values):7.1f}")
    ok = worst <= args.limit_ms
    print(f"{'PASS' if ok else 'FAIL'}: worst p95 {worst:.1f} ms (limit {args.limit_ms:.0f} ms)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
