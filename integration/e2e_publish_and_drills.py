"""Scenario j (self-healing KB: unanswered -> approve a KB entry -> Publish -> runtime reloads -> answered) and the
duplicate-delivery part of k, against the RUNNING stack. Ops must be started with RUNTIME_MODE=live (put RUNTIME_MODE=live in
integration/.env, then integration/run_all.ps1).

    python -X utf8 integration/e2e_publish_and_drills.py [--phase j|k-dups|k-down|k-up]

The KB entry created here is a clearly-marked TEST entry (no real college facts) in the throwaway local ops database.
Phases k-down / k-up are the "stop ops mid-call" drill: stop ops yourself between them (see integration/README.md).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from e2e_scenarios import ENV, OPS, RUNTIME, SVC, Call, check, events_of, kinds, login, ops_get, results, tickets_of_quick  # noqa: E402

http = httpx.Client(timeout=60)
ENTRY_ID = "TRN-901"


def runtime_kb() -> str:
    return http.get(f"{RUNTIME}/healthz").json()["brain"]["kb_version"]


def phase_j():
    admin = login("admin@demo")
    q = "Noida Sector 62 bus route"
    # 1. unanswered before
    c1 = Call("j1")
    c1.say(q)
    p1 = [e["summary"]["path"] for e in c1.events if e["type"] == "done"]
    check("j", p1 and p1[-1] in ("escalation", "slot_filling"), "before publish: the question is NOT answered (escalation path)", f"path={p1} said={' '.join(c1.spoken)[:110]}")
    kb_before = runtime_kb()
    # 2. staff drafts an entry (TEST DATA ONLY), admin approves it
    entry = {"id": ENTRY_ID, "department": "transport", "question_variants": [q, "Noida sector 62 bus kab chalti hai", "bus route to Noida Sector 62"],
             "answer_en": "This is an integration test entry for the Noida Sector 62 bus route.", "answer_hi": "Yeh Noida Sector 62 bus route ke liye ek integration test entry hai.",
             "source_title": "INTEGRATION TEST (fake data, not college information)", "source_ref": "integration/e2e_publish_and_drills.py", "tags": ["test"],
             "is_faq": True, "status": "draft"}
    r = admin.post(f"{OPS}/v1/kb/entries", json=entry)
    check("j", r.status_code == 201, "admin saved the KB entry as a draft", f"http {r.status_code} {r.text[:120] if r.status_code != 201 else ''}")
    code, ex = ops_get("/v1/kb/export")
    check("j", ENTRY_ID not in json.dumps(ex), "draft is NOT in /v1/kb/export", f"http {code}")
    r = admin.post(f"{OPS}/v1/kb/entries/{ENTRY_ID}/status", json={"status": "approved"})
    check("j", r.status_code == 200, "admin approved the entry", f"http {r.status_code}")
    code, ex = ops_get("/v1/kb/export")
    check("j", ENTRY_ID in json.dumps(ex), "approved entry is in /v1/kb/export (service key)", f"http {code}")
    r = http.post(f"{OPS}/v1/kb/publish", headers=SVC)
    check("j", r.status_code in (401, 403), "the service key cannot publish (admin only)", f"http {r.status_code}")
    # 3. publish -> ops asks the runtime to reload -> the Brain fetches /v1/kb/export with the key
    r = admin.post(f"{OPS}/v1/kb/publish")
    info = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("j", r.status_code == 200 and info.get("kb_version"), "Publish (live) returned the runtime's KBVersionInfo", json.dumps(info)[:260] if info else f"http {r.status_code} {r.text[:200]}")
    kb_after = runtime_kb()
    check("j", kb_after != kb_before and kb_after == info.get("kb_version"), "runtime/Brain now serves the new kb_version", f"{kb_before} -> {kb_after}")
    # 4. answered after
    c2 = Call("j2")
    c2.say(q)
    d = [e["summary"] for e in c2.events if e["type"] == "done"][-1]
    check("j", d["path"] == "faq" and ENTRY_ID in d["kb_ids"], "after publish: answered from the new entry", f"path={d['path']} kb_ids={d['kb_ids']} said={' '.join(c2.spoken)[:110]}")
    check("j", not tickets_of_quick(c2.id), "no ticket needed once answered")
    # 5. a failing publish (bad runtime) is reported, not swallowed: wrong key at the runtime
    r = http.post(f"{RUNTIME}/admin/kb/reload", headers={"X-Service-Key": "wrong"}, json={"kind": "url", "location": f"{OPS}/v1/kb/export"})
    check("j", r.status_code in (401, 403), "runtime /admin/kb/reload rejects a wrong service key", f"http {r.status_code}")


def phase_k_dups():
    # duplicate deliveries straight to ops, as the runtime's retry queue would send them
    cid = f"e2e-k-{uuid.uuid4().hex[:6]}"
    call = {"call_id": cid, "channel": "test", "caller_number": None, "started_at": "2026-11-02T10:00:00+05:30", "kb_version": "x", "lang": "hinglish"}
    r1 = http.post(f"{OPS}/v1/calls", headers=SVC, json=call)
    r2 = http.post(f"{OPS}/v1/calls", headers=SVC, json=call)
    check("k", (r1.status_code, r2.status_code) == (201, 200), "duplicate POST /v1/calls: 201 then 200", f"{r1.status_code},{r2.status_code}")
    t = {"idempotency_key": f"{cid}:1:HUMAN_REQUEST", "call_id": cid, "type": "HUMAN_REQUEST", "priority": "P3", "priority_reason": "R-HUMAN: test", "department": "helpdesk",
         "summary": "duplicate delivery test", "collected_fields": {}, "callback_requested": False, "transcript_excerpt": "", "brief": "dup test"}
    a = http.post(f"{OPS}/v1/tickets", headers=SVC, json=t)
    b = http.post(f"{OPS}/v1/tickets", headers=SVC, json=t)
    ja, jb = a.json(), b.json()
    check("k", a.status_code == 201 and b.status_code == 200 and ja["ticket_id"] == jb["ticket_id"] and jb["created"] is False, "duplicate ticket: 201 then 200, same ticket id, created=false", f"{a.status_code}/{b.status_code} {ja} {jb}")
    sms = {"idempotency_key": f"{cid}:ticket_created:+919876543210", "ticket_id": ja["ticket_id"], "to_number": "+919876543210", "template_id": "ticket_created",
           "vars": {"ticket_id": ja["ticket_id"], "dept": "General Helpdesk"}, "consent": "transactional"}
    s1 = http.post(f"{OPS}/v1/notifications/sms", headers=SVC, json=sms)
    s2 = http.post(f"{OPS}/v1/notifications/sms", headers=SVC, json=sms)
    check("k", (s1.status_code, s2.status_code) == (202, 200) and s1.json().get("message_id") == s2.json().get("message_id"),
          "duplicate SMS: 202 then 200 with the ORIGINAL result (nothing re-sent)", f"{s1.status_code}/{s2.status_code} {s1.text[:120]}")
    ev = [e for e in events_of(ja["ticket_id"]) if e["kind"].startswith("sms_")]
    check("k", len(ev) == 1, "exactly one sms event on the ticket after the duplicate", f"{len(ev)} sms events")
    bad = http.post(f"{OPS}/v1/tickets", headers={"X-Service-Key": "wrong"}, json=t)
    check("k", bad.status_code == 401, "wrong service key -> 401", f"{bad.status_code} {bad.text[:100]}")


def phase_k_down():
    """Run while ops is STOPPED. The runtime must still answer; tickets get a temporary id and are queued."""
    c = Call("kdown")
    t0 = time.time()
    c.say("What time does the hostel gate close?")
    check("k", "10 pm" in " ".join(c.spoken), "ops down: FAQ/LLM answer is still spoken", " ".join(c.spoken)[:100])
    c.say("mujhe kisi insaan se baat karni hai")
    said = " ".join(c.spoken)
    check("k", "create_ticket" in kinds(c.events), "ops down: the Brain still requested the ticket", str([k for k in kinds(c.events) if k != "speech"]))
    check("k", time.time() - t0 < 60, "ops down: turns do not hang", f"{time.time() - t0:.1f}s for 2 turns")
    print("INFO ops-down speech:", said[:260])
    (pathlib.Path(__file__).resolve().parent / ".run" / "kdown_call.txt").write_text(c.id, encoding="utf-8")


def phase_k_up():
    cid = (pathlib.Path(__file__).resolve().parent / ".run" / "kdown_call.txt").read_text(encoding="utf-8")
    for _ in range(20):
        t = tickets_of_quick(cid)
        if t:
            break
        time.sleep(1.5)
    check("k", bool(t), "after ops restarted: the queued ticket reached ops (retry queue flushed)", ",".join(x["ticket_id"] for x in t) or "no ticket in ops for the call")




def phase_j_workaround():
    """Same J6 flow but the reload is triggered directly (not nested inside the Publish request). Works around the ops
    'database is locked' self-call (FINDINGS_FOR_C.md #1); the runtime reload + Brain fetch + answer are verified the same."""
    before = runtime_kb()
    r = http.post(f"{RUNTIME}/admin/kb/reload", headers=SVC, json={"kind": "url", "location": f"{OPS}/v1/kb/export"})
    info = r.json() if r.status_code == 200 else {}
    check("j-workaround", r.status_code == 200 and info.get("kb_version"), "runtime /admin/kb/reload (service key) returned KBVersionInfo", json.dumps(info)[:240] if info else f"http {r.status_code} {r.text[:200]}")
    check("j-workaround", runtime_kb() != before, "Brain serves the new kb_version", f"{before} -> {runtime_kb()}")
    c = Call("jw")
    c.say("Noida Sector 62 bus route")
    d = [e["summary"] for e in c.events if e["type"] == "done"][-1]
    check("j-workaround", d["path"] == "faq" and ENTRY_ID in d["kb_ids"], "answered from the newly published entry", f"path={d['path']} kb_ids={d['kb_ids']} said={' '.join(c.spoken)[:110]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="j")
    a = ap.parse_args()
    {"j": phase_j, "j-workaround": phase_j_workaround, "k-dups": phase_k_dups, "k-down": phase_k_down, "k-up": phase_k_up}[a.phase]()
    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed of {len(results)} checks")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
