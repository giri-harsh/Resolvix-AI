"""Full-system scenarios a-i against the RUNNING stack (ops :8100, voice runtime + real Brain :8000), driven through the
runtime's typed test endpoint (POST /api/test/turn) and verified in the ops API. Start the stack first: integration/run_all.ps1.

    python -X utf8 integration/e2e_scenarios.py [--only c,d]

Reads SERVICE_KEY and SEED_PASSWORD from integration/.env (never printed). Prints PASS/FAIL with evidence; exit code 0 only if
every executed check passed. Results are also written to integration/.run/e2e_results.json.
(Scenarios j = live KB publish and k = failure drills are in e2e_publish_and_drills.py.)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time
import uuid

import httpx

HERE = pathlib.Path(__file__).resolve().parent
RUNTIME, OPS = "http://localhost:8000", "http://localhost:8100"


def load_env() -> dict[str, str]:
    out = {}
    for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


ENV = load_env()
SVC = {"X-Service-Key": ENV["SERVICE_KEY"]}
http = httpx.Client(timeout=90)
results: list[dict] = []
TURN_MS: list[tuple[int, str]] = []        # (wall ms of POST /api/test/turn, brain path)


def check(scenario: str, ok: bool, what: str, evidence: str = "") -> bool:
    results.append({"scenario": scenario, "ok": bool(ok), "check": what, "evidence": evidence[:400]})
    print(f"{'PASS' if ok else 'FAIL'} [{scenario}] {what}" + (f"  -> {evidence[:220]}" if evidence else ""))
    return bool(ok)


def kinds(events) -> list[str]:
    return [e["action"]["kind"] if e["type"] == "action" else e["type"] for e in events]


class Call:
    """One typed-test call. ``say`` returns the runtime response; ``events`` accumulates every event of the call."""

    def __init__(self, tag: str):
        self.id = f"e2e-{tag}-{uuid.uuid4().hex[:6]}"
        self.events: list[dict] = []
        self.spoken: list[str] = []

    def say(self, text: str) -> dict:
        t0 = time.perf_counter()
        r = http.post(f"{RUNTIME}/api/test/turn", json={"call_id": self.id, "text": text})
        r.raise_for_status()
        j = r.json()
        path = next((e["summary"]["path"] for e in j["events"] if e["type"] == "done"), "?")
        TURN_MS.append((int((time.perf_counter() - t0) * 1000), path))
        self.events += j["events"]
        self.spoken.append(j["spoken_text"])
        return j

    def drive(self, opener: str, replies: list[str], until: str = "create_ticket") -> bool:
        """Say the opener then the replies one by one until an event of kind ``until`` has been produced."""
        self.say(opener)
        for t in replies:
            if until in kinds(self.events):
                break
            self.say(t)
        return until in kinds(self.events)

    def done_summaries(self) -> list[dict]:
        return [e["summary"] for e in self.events if e["type"] == "done"]


def ops_get(path: str, **kw):
    r = http.get(f"{OPS}{path}", headers=SVC, **kw)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def tickets_of(call_id: str) -> list[dict]:
    for _ in range(10):                                   # the runtime posts to ops asynchronously
        code, j = ops_get("/v1/tickets", params={"call_id": call_id})
        items = [t for t in (j.get("items") if isinstance(j, dict) else []) if t["call_id"] == call_id]
        if items:
            return items
        time.sleep(0.5)
    return []


def events_of(ticket_id: str) -> list[dict]:
    code, j = ops_get(f"/v1/tickets/{ticket_id}/events")
    return j.get("items", j) if isinstance(j, dict) else j


class Stream(threading.Thread):
    """Collects /v1/stream SSE events (admin session) so alert.p1 etc. can be asserted."""

    def __init__(self, cookies):
        super().__init__(daemon=True)
        self.cookies, self.seen, self.stop = cookies, [], False

    def run(self):
        try:
            with httpx.Client(timeout=None, cookies=self.cookies) as c, c.stream("GET", f"{OPS}/v1/stream") as r:
                name = None
                for line in r.iter_lines():
                    if self.stop:
                        return
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:") and name:
                        try:
                            self.seen.append((name, json.loads(line[5:])))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            return


def login(email: str) -> httpx.Client:
    c = httpx.Client(timeout=30)
    r = c.post(f"{OPS}/v1/auth/login", json={"email": email, "password": ENV["SEED_PASSWORD"]})
    r.raise_for_status()
    return c


# ----------------------------------------------------------------------------------------------------------- scenarios
def s_a():
    for lang, q, expect in (("hi", "B.Tech second year ki semester fee ki last date kya hai?", "30 November"),
                            ("en", "What time does the hostel gate close?", "10 pm")):
        c = Call("a" + lang)
        c.say(q)
        text = " ".join(c.spoken)
        check("a", expect.lower() in text.lower(), f"FAQ answer ({lang}) contains '{expect}'", text[:140])
        time.sleep(1.5)
        code, j = ops_get(f"/v1/calls/{c.id}")
        check("a", code == 200 and len(j["turns"]) >= 1 and j["call"]["call_id"] == c.id, f"call + turn stored in ops ({lang})",
              f"http {code}, turns={len(j.get('turns', [])) if isinstance(j, dict) else '?'} path={j['turns'][0]['path'] if code == 200 and j['turns'] else None}")
        check("a", not tickets_of_quick(c.id), f"no ticket for an answered question ({lang})")


def tickets_of_quick(call_id):
    code, j = ops_get("/v1/tickets", params={"call_id": call_id})
    return [t for t in (j.get("items") if isinstance(j, dict) else []) if t["call_id"] == call_id]


def s_b():
    c = Call("b")
    done = c.drive("Noida sector 62 ke liye bus route kya hai?", ["haan", "9876543210", "haan isi number par call kar dena"], until="log_gap")
    check("b", done, "Brain requested log_gap for an unanswered question", " ".join(c.spoken)[:160])
    t = tickets_of(c.id)
    check("b", any(x["type"] == "KNOWLEDGE_GAP" for x in t), "KNOWLEDGE_GAP ticket created", ",".join(x["type"] + ":" + x["ticket_id"] for x in t))
    time.sleep(1)
    code, j = ops_get("/v1/gaps/clusters")
    items = j.get("items", j) if isinstance(j, dict) else j
    check("b", code == 200 and any("bus" in json.dumps(i, ensure_ascii=False).lower() or "noida" in json.dumps(i, ensure_ascii=False).lower() for i in items),
          "gap clustered in ops (/v1/gaps/clusters)", f"{len(items)} clusters")


def s_c(stream):
    c = Call("c")
    c.drive("Mera exam 15 minute mein hai aur exam hall nahi mil raha", ["mera roll number 2400270130012 hai", "paper BCS501 hai", "haan"])
    t = tickets_of(c.id)
    p1 = [x for x in t if x["priority"] == "P1" and x["type"] == "URGENT"]
    check("c", bool(p1), "P1 URGENT ticket created", ",".join(f"{x['type']}/{x['priority']}/{x['department']}/{x['ticket_id']}" for x in t))
    k = kinds(c.events)
    if "transfer" in k and "create_ticket" in k:
        check("c", k.index("create_ticket") < k.index("transfer"), "ticket-first: create_ticket before transfer", str([x for x in k if x != "speech"]))
    else:
        check("c", "create_ticket" in k, "ticket created (no transfer: office closed -> callback)", str([x for x in k if x != "speech"]))
    if p1:
        check("c", p1[0]["department"] == "exam_cell", "routed to exam_cell")
        time.sleep(1)
        check("c", any(n == "alert.p1" for n, d in stream.seen), "alert.p1 received on /v1/stream", f"stream events seen: {sorted({n for n, _ in stream.seen})}")
        ev = events_of(p1[0]["ticket_id"])
        check("c", True, "ticket timeline (events)", ",".join(e["kind"] for e in ev))


def s_d(stream):
    c = Call("d")
    c.say("ek ladki behosh ho gayi hai hostel mein")
    t = tickets_of(c.id)
    check("d", any(x["type"] == "EMERGENCY" and x["priority"] == "P1" and x["department"] == "emergency" for x in t), "P1 EMERGENCY ticket",
          ",".join(f"{x['type']}/{x['priority']}/{x['ticket_id']}" for x in t))
    k = kinds(c.events)
    check("d", k.index("create_ticket") < k.index("transfer") if "transfer" in k else False, "emergency: ticket before transfer", str([x for x in k if x != "speech"]))
    check("d", "112" in " ".join(c.spoken) or "emergency" in " ".join(c.spoken).lower(), "emergency script spoken", " ".join(c.spoken)[:140])
    time.sleep(1)
    mine = [d for n, d in stream.seen if n == "alert.p1" and json.dumps(d).find(t[0]["ticket_id"]) >= 0] if t else []
    check("d", bool(mine), "alert.p1 for the emergency ticket on /v1/stream", f"alert.p1 events={sum(1 for n, d in stream.seen if n == 'alert.p1')}, for {t[0]['ticket_id'] if t else '?'}: {len(mine)}")
    sms = [k for k in sms_keys_of(c.id)]
    check("d", sum(1 for k in sms if k.startswith("staff_alert")) == 2, "two staff_alert SMS (mock) recorded, one per configured number", str(sms))


def s_e():
    c = Call("e")
    c.say("mujhe kisi insaan se baat karni hai")
    t = tickets_of(c.id)
    check("e", any(x["type"] == "HUMAN_REQUEST" for x in t), "HUMAN_REQUEST ticket", ",".join(x["ticket_id"] for x in t))
    k = kinds(c.events)
    check("e", "transfer" in k or "callback" in " ".join(c.spoken).lower() or "call karega" in " ".join(c.spoken).lower(), "transfer or callback offered", " ".join(c.spoken)[:150])


def s_f():
    c = Call("f")
    ok = c.drive("Hostel ka wifi do din se nahi chal raha", ["Block B room 214", "haan", "9876543210", "haan", "haan sahi hai", "haan"])
    t = tickets_of(c.id)
    cm = [x for x in t if x["type"] == "COMPLAINT"]
    check("f", ok and bool(cm), "COMPLAINT ticket after read-back", " | ".join(s[:60] for s in c.spoken[:6]))
    if cm:
        cf = cm[0]["collected_fields"]
        check("f", "214" in json.dumps(cf), "collected_fields has the location", json.dumps(cf, ensure_ascii=False))
    return c, (cm[0] if cm else None)


def s_g():
    c = Call("g")
    c.say("Admission ke liye kaunse documents chahiye?")      # mock LLM times out on this keyword -> R-FALLBACK
    t = tickets_of(c.id)
    check("g", any(x["type"] == "SYSTEM_FAILURE" for x in t), "SYSTEM_FAILURE ticket (R-FALLBACK)", ",".join(f"{x['type']}/{x['priority']}" for x in t))
    d = c.done_summaries()
    check("g", d and d[-1]["path"] == "fallback" and d[-1]["rule_id"] == "R-FALLBACK", "turn summary path=fallback rule=R-FALLBACK", json.dumps({k: d[-1].get(k) for k in ("path", "rule_id")}) if d else "")
    check("g", "call" in " ".join(c.spoken).lower() or "sorry" in " ".join(c.spoken).lower(), "callback line spoken", " ".join(c.spoken)[:140])


def s_h(complaint_call: Call):
    """CR-7: a second ticket in the same call to the same number must not claim or create a second SMS."""
    sms_before = sms_count(complaint_call.id)
    c = complaint_call
    c.say("Noida sector 62 ke liye bus route kya hai?")
    for t in ["haan", "9876543210", "haan isi number par call kar dena"]:
        if "log_gap" in kinds(c.events):
            break
        c.say(t)
    time.sleep(1.5)
    sms_after = sms_count(c.id)
    sms_keys = sms_keys_of(c.id)
    check("h", sms_before == 1, "first ticket_created SMS recorded (mode mock)", f"count={sms_before} keys={sms_keys}")
    check("h", sms_after == 1, "second ticket in the same call did not create a second SMS (CR-7)", f"before={sms_before} after={sms_after}")
    second = [t for t in tickets_of_quick(c.id) if t["type"] == "KNOWLEDGE_GAP"]
    check("h", bool(second), "the second ticket (KNOWLEDGE_GAP) was created in the same call", ",".join(t["ticket_id"] for t in second))
    ticket_turn = next((i for i in range(len(c.spoken) - 1, -1, -1) if "{{" not in c.spoken[i] and "ticket" in c.spoken[i].lower()), len(c.spoken) - 1)
    said = c.spoken[ticket_turn].lower()
    check("h", "sms" not in said, "no SMS claim spoken in the second ticket's confirmation", said[:160])


def sms_keys_of(call_id: str) -> list[str]:
    """SMS outcomes are ticket events (sms_queued / sms_sent / sms_failed / sms_suppressed) carrying template_id and status."""
    out = []
    for t in tickets_of_quick(call_id):
        for e in events_of(t["ticket_id"]) or []:
            if e["kind"].startswith("sms_"):
                p = e.get("payload", {})
                out.append(f"{p.get('template_id')}:{p.get('status')}:{p.get('mode')}")
    return out


def sms_count(call_id: str) -> int:
    return len([k for k in sms_keys_of(call_id) if k.startswith("ticket_created")])


def s_i(ticket):
    if not ticket:
        return check("i", False, "no complaint ticket to work on (scenario f failed)")
    staff = login("hostel@demo")
    tid = ticket["ticket_id"]
    before = [k for k in sms_keys_of(ticket["call_id"]) if k.startswith("status_change")]
    r = staff.patch(f"{OPS}/v1/tickets/{tid}", json={"status": "in_progress"})
    check("i", r.status_code == 200, "staff (hostel@demo) set status in_progress", f"http {r.status_code}")
    time.sleep(1.5)
    after = [k for k in sms_keys_of(ticket["call_id"]) if k.startswith("status_change")]
    check("i", len(after) - len(before) == 1, "exactly one status_change SMS (mock) recorded", f"before={before} after={after}")
    r = staff.patch(f"{OPS}/v1/tickets/{tid}", json={"status": "in_progress"})
    time.sleep(1)
    again = [k for k in sms_keys_of(ticket["call_id"]) if k.startswith("status_change")]
    check("i", len(again) == len(after), "repeating the same status does not send another SMS", f"{len(after)} -> {len(again)}")
    other = login("exam@demo")
    r = other.patch(f"{OPS}/v1/tickets/{tid}", json={"status": "resolved"})
    check("i", r.status_code in (403, 404), "staff of another department cannot change it", f"http {r.status_code}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    want = lambda s: not only or s in only  # noqa: E731

    h = http.get(f"{RUNTIME}/healthz").json()
    check("0", bool(h["brain"].get("embedder")) and h["ops_api"] == "ok", "runtime is using the REAL Brain and ops is reachable",
          f"brain.embedder={h['brain'].get('embedder')} kb={h['brain'].get('kb_version')} llm_mode={h['brain'].get('llm_mode')} ops={h['ops_api']}")
    admin = login("admin@demo")
    stream = Stream(admin.cookies)
    stream.start()
    time.sleep(1.0)
    if want("a"): s_a()
    if want("b"): s_b()
    if want("c"): s_c(stream)
    if want("d"): s_d(stream)
    if want("e"): s_e()
    fc, ft = (None, None)
    if want("f") or want("h") or want("i"):
        fc, ft = s_f()
    live = h["brain"].get("llm_mode") == "live"
    if want("g") and not live: s_g()
    elif want("g"): print("SKIP [g] needs the mock LLM's timeout trigger; with a live LLM the fallback path cannot be forced from text")
    if want("h") and fc: s_h(fc)
    if want("i"): s_i(ft)
    stream.stop = True
    (HERE / ".run").mkdir(exist_ok=True)
    (HERE / ".run" / "e2e_results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")
    def pct(v, q):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * q))] if v else 0

    by: dict[str, list[int]] = {}
    for ms, path in TURN_MS:
        by.setdefault(path, []).append(ms)
    print(f"\nturn wall time through the runtime, ms  [llm_mode={h['brain'].get('llm_mode', '?')}]")
    for path, v in sorted(by.items()):
        print(f"  {path:<12} n={len(v):<3} p50={pct(v, .5)} p95={pct(v, .95)} max={max(v)}")
    allv = [m for m, _ in TURN_MS]
    print(f"  {'all':<12} n={len(allv):<3} p50={pct(allv, .5)} p95={pct(allv, .95)} max={max(allv) if allv else 0}")
    (HERE / ".run" / "e2e_latency.json").write_text(json.dumps({k: {"n": len(v), "p50": pct(v, .5), "p95": pct(v, .95), "max": max(v)} for k, v in by.items()}, indent=1), encoding="utf-8")
    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed of {len(results)} checks")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
