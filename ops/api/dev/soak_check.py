"""Soak test: replay the 9 simulator scenarios many times against a real uvicorn + file database and watch resources.

Run with: python -X utf8 dev/soak_check.py [--cycles 60] [--speed 0] [--minutes N]
  --speed 0     no pauses (compressed run); --speed 5 is the natural demo pacing (one cycle takes a few minutes)
  --minutes N   keep cycling until N minutes passed (overrides --cycles)
An SSE client stays connected during the whole run. Every cycle samples the database size, the WAL size, the
server thread count and the number of SSE subscribers; the report compares the first and the last sample and
prints PASS when the growth is small (no leak) and every event kept arriving.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

OPS_API_DIR = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(Path(__file__).resolve().parent), str(OPS_API_DIR)]
from sim_lib import run_simulation  # noqa: E402


def free_port() -> int:
    wanted = int(os.environ.get("SMOKE_PORT", "8100"))
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", wanted)) != 0:
            return wanted
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_pid(launcher_pid: int) -> int:
    """The venv python.exe is a launcher that spawns the real interpreter: measure the child when there is one."""
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={launcher_pid}' | Select-Object -First 1).ProcessId"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else launcher_pid


def thread_count(pid: int) -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid}).Threads.Count"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else -1


def handle_count(pid: int) -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid}).HandleCount"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=60)
    ap.add_argument("--speed", type=float, default=0)
    ap.add_argument("--minutes", type=float, default=0)
    args = ap.parse_args()
    key = next(l.split("=", 1)[1].strip() for l in (OPS_API_DIR / ".env").read_text(encoding="utf-8").splitlines()
               if l.startswith("SERVICE_KEY="))
    port = free_port()
    db_name = f"soak_{uuid.uuid4().hex[:8]}.db"
    db_path = OPS_API_DIR / "data" / db_name
    env = dict(os.environ, DATABASE_URL=f"sqlite:///./data/{db_name}", NOTIFY_MODE="mock", PORT=str(port))
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "uvicorn", "resolvix_ops.app:app", "--port", str(port),
                             "--loop", "asyncio", "--workers", "1"], cwd=OPS_API_DIR, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Service-Key": key}
    client = httpx.Client(base_url=base, headers=headers, timeout=20.0)
    events = {"n": 0, "alive": True}

    def listen():
        try:
            with httpx.Client(base_url=base, timeout=httpx.Timeout(10.0, read=300.0)) as c:
                with c.stream("GET", "/v1/stream", headers=headers) as r:
                    for line in r.iter_lines():
                        if line.startswith("event:"):
                            events["n"] += 1
        except httpx.HTTPError:
            events["alive"] = False

    samples = []
    try:
        for _ in range(80):
            try:
                if client.get("/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.25)
        threading.Thread(target=listen, daemon=True).start()
        time.sleep(1)

        real_pid = server_pid(proc.pid)

        def sample(label: str) -> dict:
            wal = Path(f"{db_path}-wal")
            return {"label": label, "t": time.time(), "db_kb": db_path.stat().st_size // 1024,
                    "wal_kb": (wal.stat().st_size // 1024) if wal.exists() else 0,
                    "threads": thread_count(real_pid), "handles": handle_count(real_pid), "events": events["n"]}

        samples.append(sample("start"))
        started = time.time()
        calls = tickets = 0
        cycle = 0
        while True:
            cycle += 1
            results = run_simulation(client, speed=args.speed, run_id=f"s{cycle}", out=lambda _l: None)
            calls += len(results)
            tickets += sum(len(r.tickets) for r in results)
            if not all(r.ok for r in results):
                print(f"FAIL: cycle {cycle} had a failed scenario")
                return 1
            if cycle % 5 == 0 or cycle == 1:
                samples.append(sample(f"cycle {cycle}"))
            done = (time.time() - started) / 60 >= args.minutes if args.minutes else cycle >= args.cycles
            if done:
                break
        time.sleep(1)
        samples.append(sample("end"))
        sse_alive = events["alive"]  # read before the server is stopped
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)
        for suffix in ("", "-wal", "-shm"):
            for _ in range(20):
                try:
                    Path(f"{db_path}{suffix}").unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.25)

    print(f"{'sample':12} {'db KB':>8} {'wal KB':>8} {'threads':>8} {'handles':>8} {'sse events':>11}")
    for s in samples:
        print(f"{s['label']:12} {s['db_kb']:8d} {s['wal_kb']:8d} {s['threads']:8d} {s['handles']:8d} {s['events']:11d}")
    first, last = samples[1], samples[-1]  # after the first cycle (warm) vs the end
    mins = (samples[-1]["t"] - samples[0]["t"]) / 60
    print(f"{cycle} cycles, {calls} calls, {tickets} tickets in {mins:.1f} min; sse client alive: {sse_alive}")
    problems = []
    if last["threads"] > first["threads"] + 5:
        problems.append(f"threads grew {first['threads']} -> {last['threads']}")
    if last["handles"] > first["handles"] * 1.5 + 50:
        problems.append(f"handles grew {first['handles']} -> {last['handles']}")
    if last["wal_kb"] > 8192:
        problems.append(f"WAL is {last['wal_kb']} KB")
    if not sse_alive or last["events"] <= first["events"]:
        problems.append("SSE stopped delivering events")
    print("PASS: no leak detected" if not problems else "FAIL: " + "; ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
