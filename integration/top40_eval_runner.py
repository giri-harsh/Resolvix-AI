"""Top-40 question evaluation through the REAL brain (runtime typed turn API, live Groq).

One command:   python integration/top40_eval_runner.py [--eval kb-data/top40_eval.jsonl] [--runtime http://localhost:8000] [--out integration/TOP40_RESULTS.md]

Input rows (jsonl), flexible on purpose (Harsh's file is the authority; unknown fields are ignored):
  one utterance per row : {"id": "Q07", "utterance": "...", "lang": "en|hi|hinglish", "expected_entry_id": "FEE-012", "must_contain": ["30 November"]}
  or several languages  : {"id": "Q07", "utterances": {"en": "...", "hi": "...", "hinglish": "..."}, "expected_entry_id": ..., "must_contain": [...]}
  aliases accepted      : utterance|text|question|query ; expected_entry_id|entry_id|expected_id ; must_contain may be a string or a list
                          (a list item can be "a|b" = either), must_not_contain optional (a wrong figure must never appear).

PASS = the turn cites expected_entry_id (kb_ids) AND the spoken answer contains every must_contain item AND no must_not_contain item.
Miss classes:
  RETRIEVAL  expected entry not cited and the brain answered from something else / nothing
  VALIDATION a validator blocked or rewrote the answer (validator_flags other than benign ones)
  ESCALATED  answerable but the brain escalated / fell back (ticket, "no verified info")
  LLM        right entry, wrong wording / language / missing must_contain, or the LLM timed out
  DATA       the entry itself (as published in ops) does not contain the must_contain text
A wrong figure (must_not_contain) is reported as WRONG-ANSWER: never acceptable.
Groq rate limits / timeouts: the runner retries a turn (backoff) when the runtime answers 5xx or the brain reports llm_timeout.
The run does NOT leave open calls: every call is ended through /api/test/end. Tickets created by escalations stay in the demo ops (documented in the report).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "brain"))
try:                                                    # the same TTS formatting the brain applies to every reply
    from resolvix_brain.speech import to_spoken
except Exception:                                       # pragma: no cover - runner still works without the brain package
    def to_spoken(text: str, lang: str, *a, **k) -> str:
        return text
LANGS = ("en", "hi", "hinglish")
BENIGN_FLAGS = {"json_retry"}
RETRY_FLAGS = {"llm_timeout", "llm_error", "llm_empty"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.:/%-]", " ", text)).strip()


def first(row: dict, *names, default=None):
    for n in names:
        if row.get(n) not in (None, "", []):
            return row[n]
    return default


def as_list(v) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def contains(answer: str, item: str) -> bool:
    """must_contain is written the way the entry is DISPLAYED ("1,42,156", "Rs 200"); the brain answers in spoken form
    ("1 lakh 42 thousand 156 rupees"), so an item also matches through the brain's own speech formatting (en and hi)."""
    a = norm(answer)
    for alt in item.split("|"):
        if not alt.strip():
            continue
        forms = {alt, to_spoken(alt, "en"), to_spoken(alt, "hi")}
        money = re.match(r"^\s*(?:rs\.?|inr|₹)\s*([\d,]+)\s*$", alt, re.I)
        if money:                                       # "Rs 200" is spoken as "200 rupees" / "200 rupaye"
            forms |= {f"{money.group(1)} rupees", f"{money.group(1)} rupaye", to_spoken(f"Rs {money.group(1)}", "en")}
        if any(norm(form) in a for form in forms):
            return True
    return False


def load_rows(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            sys.exit(f"{path}:{n}: not valid JSON ({exc})")
    return rows


def expand(rows: list[dict]) -> list[dict]:
    """-> one item per (question, language)."""
    items = []
    for i, row in enumerate(rows, 1):
        qid = str(first(row, "id", "qid", "q", "question_id", default=f"Q{i:02d}"))
        expected = first(row, "expected_entry_id", "entry_id", "expected_id")
        must = as_list(first(row, "must_contain", "contains", default=[]))
        must_not = as_list(first(row, "must_not_contain", default=[]))
        multi = first(row, "utterances", "variants")
        if isinstance(multi, dict):
            for lang, text in multi.items():
                if text:
                    items.append(dict(qid=qid, lang=str(lang).lower(), text=text, expected=expected, must=must, must_not=must_not))
        else:
            text = first(row, "utterance", "text", "question", "query")
            if text:
                items.append(dict(qid=qid, lang=str(first(row, "lang", "language", default="hinglish")).lower(), text=text,
                                  expected=expected, must=must, must_not=must_not))
    return items


class Runner:
    def __init__(self, runtime: str, ops: str, service_key: str | None, retries: int) -> None:
        self.http = httpx.Client(base_url=runtime.rstrip("/"), timeout=40)
        self.ops = httpx.Client(base_url=ops.rstrip("/"), timeout=10, headers={"X-Service-Key": service_key or ""}) if service_key else None
        self.retries = retries
        self._kb: dict[str, str] | None = None
        self._answers: dict[str, list[str]] = {}

    def kb_text(self, entry_id: str | None) -> str | None:
        """Published answer text of one entry (for DATA classification); None when ops is not reachable."""
        if not entry_id or self.ops is None:
            return None
        if self._kb is None:
            self._kb = {}
            try:
                data = self.ops.get("/v1/kb/entries", params={"limit": 500}).json()
                for e in data.get("items", data) if isinstance(data, dict) else data:
                    self._kb[e["id"]] = " ".join(str(e.get(k) or "") for k in ("answer_en", "answer_hi"))
                    self._answers[e["id"]] = [str(e.get(k) or "") for k in ("answer_en", "answer_hi")]
            except Exception:
                self._kb = {}
        return self._kb.get(entry_id)

    def verbatim(self, entry_id: str | None, spoken: str, lang: str) -> bool:
        """True when the reply IS the expected entry's own answer (FAQ fast path, spoken form, either language). That text is what Harsh
        reviewed, so the facts in it are right even when the must_contain string is written in another language/script."""
        if not entry_id or self.kb_text(entry_id) is None:
            return False
        reply = norm(spoken)
        for ans in self._answers.get(entry_id, []):
            head = norm(to_spoken(ans, lang))[:60]
            if len(head) >= 20 and head in reply:
                return True
        return False

    def turn(self, text: str) -> dict:
        last = {}
        for attempt in range(self.retries + 1):
            try:
                t0 = time.perf_counter()
                r = self.http.post("/api/test/turn", json={"text": text})
                ms = int((time.perf_counter() - t0) * 1000)
                if r.status_code >= 500:
                    raise RuntimeError(f"runtime {r.status_code}")
                body = r.json()
                done = next((e["summary"] for e in body["events"] if e["type"] == "done"), {})
                meta = next((e for e in body["events"] if e["type"] == "meta"), {})
                last = dict(call_id=body["call_id"], spoken=body["spoken_text"], summary=done, meta=meta, ms=ms, ticket=body.get("ticket_id"))
                self.http.post("/api/test/end", json={"call_id": body["call_id"]})
                flags = set(done.get("validator_flags") or [])
                if flags & RETRY_FLAGS and attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    last["retried"] = attempt + 1
                    continue
                return last
            except Exception as exc:
                last = dict(error=f"{type(exc).__name__}: {exc}", spoken="", summary={}, meta={}, ms=0)
                time.sleep(1.5 * (attempt + 1))
        return last

    def judge(self, item: dict) -> dict:
        res = self.turn(item["text"])
        s, spoken = res.get("summary", {}), res.get("spoken", "")
        cited = list(s.get("kb_ids") or [])
        flags = [f for f in (s.get("validator_flags") or [])]
        out = dict(item, answer=spoken, kb_ids=cited, path=s.get("path"), tier=s.get("tier"), reply_lang=s.get("lang"), flags=flags,
                   ms=res.get("ms"), first_audio_budget_ms=(s.get("latency_ms") or {}).get("brain_total"), ticket=res.get("ticket"),
                   retried=res.get("retried", 0))
        if res.get("error"):
            return dict(out, verdict="FAIL", miss="LLM", why=res["error"])
        bad = [m for m in item["must_not"] if contains(spoken, m)]
        if bad:
            return dict(out, verdict="FAIL", miss="WRONG-ANSWER", why=f"contains forbidden text {bad}")
        cited_ok = (item["expected"] in cited) if item["expected"] else True
        missing = [m for m in item["must"] if not contains(spoken, m)]
        if cited_ok and not missing:
            return dict(out, verdict="PASS", miss="", why="", basis="must_contain")
        if cited_ok and missing and item["expected"] and self.verbatim(item["expected"], spoken, s.get("lang") or "en"):
            return dict(out, verdict="PASS", miss="", why="", basis="entry verbatim")
        path = (s.get("path") or "")
        blocking = [f for f in flags if f not in BENIGN_FLAGS]
        if cited_ok and missing:                                   # right entry, wrong words
            data = self.kb_text(item["expected"])
            if data is not None and any(not contains(data, m) for m in missing):
                return dict(out, verdict="FAIL", miss="DATA", why=f"entry {item['expected']} does not contain {missing}")
            return dict(out, verdict="FAIL", miss="LLM", why=f"missing {missing}")
        if blocking and path in ("fallback", "escalation", "escalate"):
            klass = "LLM" if set(blocking) & RETRY_FLAGS else "VALIDATION"
            return dict(out, verdict="FAIL", miss=klass, why=f"flags {blocking}")
        if blocking:
            return dict(out, verdict="FAIL", miss="VALIDATION", why=f"flags {blocking}")
        if path in ("escalation", "fallback", "slot_filling", "safety"):
            return dict(out, verdict="FAIL", miss="ESCALATED", why=f"path {path}, cited {cited or 'nothing'}")
        return dict(out, verdict="FAIL", miss="RETRIEVAL", why=f"cited {cited or 'nothing'}, expected {item['expected']}")


def write_report(results: list[dict], out: Path, src: Path, runtime: str) -> None:
    by_lang: dict[str, list[dict]] = defaultdict(list)
    by_q: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in results:
        by_lang[r["lang"]].append(r)
        by_q[r["qid"]][r["lang"]] = r
    lines = [f"# Top-40 results", "", f"Run {time.strftime('%Y-%m-%d %H:%M:%S')} against {runtime} (real brain, live LLM), source {src.name}. "
             f"{len({r['qid'] for r in results})} questions, {len(results)} utterances.", "",
             "Re-run: `python integration/top40_eval_runner.py`", "", "## Pass rate per language", "", "| language | pass | rate |", "|---|---|---|"]
    for lang in sorted(by_lang):
        p = sum(r["verdict"] == "PASS" for r in by_lang[lang])
        lines.append(f"| {lang} | {p}/{len(by_lang[lang])} | {100 * p // max(1, len(by_lang[lang]))}% |")
    total_p = sum(r["verdict"] == "PASS" for r in results)
    strict = sum(r["verdict"] == "PASS" and r.get("basis") == "must_contain" for r in results)
    lines += ["", f"Overall {total_p}/{len(results)} ({strict} by must_contain text, {total_p - strict} because the reply is the expected entry's own reviewed answer in another language/script). Wrong answers (forbidden text): {sum(r['miss'] == 'WRONG-ANSWER' for r in results)} (must be 0).", ""]
    classes = defaultdict(int)
    for r in results:
        if r["verdict"] != "PASS":
            classes[r["miss"]] += 1
    lines += ["## Miss classes", "", " ".join(f"**{k}** {v}" for k, v in sorted(classes.items())) or "none", "",
              "## Per question", "", "| Q | " + " | ".join(LANGS) + " | miss detail |", "|---|" + "---|" * (len(LANGS) + 1)]
    for qid, langs in by_q.items():
        cells, detail = [], []
        for lang in LANGS:
            r = langs.get(lang)
            cells.append("-" if r is None else ("PASS" if r["verdict"] == "PASS" else f"FAIL {r['miss']}"))
            if r and r["verdict"] != "PASS":
                detail.append(f"{lang}: {r['why']} | said: {r['answer'][:90]!r}")
        lines.append(f"| {qid} | " + " | ".join(cells) + " | " + "; ".join(detail).replace("|", "/") + " |")
    lat = sorted(r["ms"] for r in results if r.get("ms"))
    if lat:
        lines += ["", "## Turn latency (typed path, request to full reply)", "",
                  f"median {lat[len(lat) // 2]} ms, p90 {lat[int(len(lat) * 0.9)]} ms, max {lat[-1]} ms"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", default=str(ROOT / "kb-data" / "top40_eval.jsonl"))
    ap.add_argument("--runtime", default="http://localhost:8000")
    ap.add_argument("--ops", default="http://localhost:8100")
    ap.add_argument("--out", default=str(ROOT / "integration" / "TOP40_RESULTS.md"))
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--only", help="comma separated question ids")
    ap.add_argument("--json", help="also write the raw results here")
    args = ap.parse_args()
    src = Path(args.eval)
    if not src.exists():
        sys.exit(f"eval file not found: {src} (pull kb-data from member-c-ops)")
    key = os.environ.get("SERVICE_KEY")
    envfile = ROOT / "integration" / ".env"
    if not key and envfile.exists():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("SERVICE_KEY="):
                key = line.split("=", 1)[1].strip()
    items = expand(load_rows(src))
    if args.only:
        keep = {q.strip() for q in args.only.split(",")}
        items = [i for i in items if i["qid"] in keep]
    runner = Runner(args.runtime, args.ops, key, args.retries)
    health = runner.http.get("/healthz").json()
    if not health.get("brain", {}).get("embedder"):
        sys.exit("runtime is not running the real brain (health.brain has no embedder)")
    runner.turn("hello")                                        # warm-up: the first live LLM call is slow
    results = []
    for n, item in enumerate(items, 1):
        r = runner.judge(item)
        results.append(r)
        print(f"{n:3d}/{len(items)} {item['qid']:<6} {item['lang']:<9} {r['verdict']:<4} {r.get('miss', ''):<12} {r.get('ms')} ms  {r.get('why', '')[:70]}", flush=True)
    write_report(results, Path(args.out), src, args.runtime)
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    passed = sum(r["verdict"] == "PASS" for r in results)
    print(f"\n{passed}/{len(results)} passed; report: {args.out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
