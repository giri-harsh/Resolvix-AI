"""Deterministic urgency *fact* extraction (no priority decisions: those belong to ``rules.py``).

Used in two places:
* before the LLM, so an urgent caller ("exam 15 minute mein hai, room nahi mil raha") is handled even when the
  LLM is slow or down, and cannot be mis-answered by the FAQ cache (their sentence is semantically close to
  the "seating plan" FAQ);
* merged with the LLM's own ``urgency_facts`` (see ``merge_facts``): any source can raise urgency, none can
  lower another source's safety flag.

Time phrases are the only thing converted to ``minutes_to_event``. Words like "kal" are ambiguous in Hindi
(yesterday/tomorrow) and are left to the LLM.
"""
from __future__ import annotations

import re
from typing import Any

from resolvix_contracts.models import UrgencyFacts

from .speech import _ONES, _MULT  # number words
from .text import normalize, redact, tokens

_MIN_UNITS = frozenset("minute minutes min mins mint minat मिनट".split())
_HOUR_UNITS = frozenset("hour hours hr hrs ghante ghanta ghanta घंटे घंटा".split())
_EXAM_WORDS = frozenset("exam exams paper pariksha paryiksha परीक्षा एग्जाम test viva practical lab internal".split())
_ADMIT_PHRASES = ("admit card", "hall ticket", "एडमिट कार्ड", "admit")
_FEE_WORDS = frozenset("fee fees fine फीस शुल्क".split())
_FORM_WORDS = frozenset("form application फॉर्म".split())
_DEADLINE_PHRASES = ("last date", "deadline", "aakhri", "आखिरी", "tak jama", "due date", "last day", "closing")
_BLOCKED_PHRASES = (
    "nahi mil raha", "nahi mil rahi", "nahi mil pa", "nahi mila", "nahi mili", "nahi dikh", "cannot find",
    "can't find", "cant find", "could not find", "unable", "not able", "nahi ho raha", "nahi ho pa", "nahi khul",
    "not opening", "not loading", "not working", "stuck", "lost my", "nahi pahunch", "cannot access", "can't access",
    "nahi chal raha", "nahi chal rahi", "error aa raha", "locked", "missing", "gum ho", "bhool gaya", "bhul gaya",
    "नहीं मिल रहा", "नहीं मिल रही", "नहीं खुल", "नहीं हो रहा",
)
_COMPLAINT_WORDS = ("complaint", "complain", "shikayat", "शिकायत", "shikayat darj")
_MALFUNCTION = ("nahi chal raha", "nahi chal rahi", "nahi chal", "kaam nahi kar", "not working", "band hai",
                "kharab", "broken", "out of order", "leak", "nahi aa raha", "nahi aa rahi", "nahi mil raha pani",
                "नहीं चल", "काम नहीं", "खराब")
_FACILITY = frozenset(
    "wifi internet network mess canteen water pani bijli light lights ac fan lift washroom toilet geyser room "
    "hostel bus lab projector computer cleaning cleanliness food power electricity warden gate library "
    "वाईफाई इंटरनेट पानी बिजली".split()
)
_SELF = frozenset("mujhe mera meri mere main mai i my me myself मुझे मेरा मेरी मेरे मैं".split())
_WARD = frozenset("beta beti son daughter bacha bachcha bachche ward मेरा बेटा मेरी बेटी".split())


def _number_before(toks: list[str], i: int) -> float | None:
    """Value of the number (digits or number word) immediately before toks[i], with simple 'N sau' handling."""
    if i == 0:
        return None
    p = toks[i - 1]
    if p.replace(".", "", 1).isdigit():
        return float(p)
    if p in _ONES:
        return float(_ONES[p])
    if p in ("aadha", "half", "adha", "आधा"):
        return 0.5
    if p in _MULT and i >= 2:
        base = _number_before(toks, i - 1) or 1.0
        return base * _MULT[p]
    return None


def minutes_to_event(text: str) -> int | None:
    """Minutes from phrases like "15 minute mein", "in 30 minutes", "ek ghante mein", "aadha ghanta"."""
    toks = tokens(text)
    best: int | None = None
    for i, t in enumerate(toks):
        mult = 1 if t in _MIN_UNITS else 60 if t in _HOUR_UNITS else 0
        if not mult:
            continue
        n = _number_before(toks, i)
        if n is None:
            continue
        m = int(round(n * mult))
        best = m if best is None else min(best, m)
    return best


def has_phrase(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    t = " " + normalize(text) + " "
    return any((" " + normalize(p) + " ") in t for p in phrases)


def extract_facts(text: str) -> dict[str, Any]:
    """Deterministic ``UrgencyFacts`` dict for one utterance (event_type 'none' when nothing matches)."""
    toks = set(tokens(text))
    mins = minutes_to_event(text)
    blocked = has_phrase(text, _BLOCKED_PHRASES)
    event = "none"
    if toks & _EXAM_WORDS and mins is not None and mins <= 60:
        event = "exam"            # "exam 15 minute mein hai, admit card nahi mil raha": an exam within the R-EXAM-60 window outranks the admit-card wording
    elif has_phrase(text, _ADMIT_PHRASES):
        event = "admit_card"
    elif toks & _EXAM_WORDS and blocked:
        event = "exam"
    elif toks & _FEE_WORDS and has_phrase(text, _DEADLINE_PHRASES):
        event = "fee_deadline"
    elif toks & _FORM_WORDS and has_phrase(text, _DEADLINE_PHRASES):
        event = "form_deadline"
    elif has_phrase(text, _COMPLAINT_WORDS) or (has_phrase(text, _MALFUNCTION) and toks & _FACILITY):
        event = "complaint"
    affected = "self" if toks & _SELF else ("ward" if toks & _WARD else None)
    return {
        "event_type": event,
        "minutes_to_event": mins if event != "none" else None,
        "blocked": bool(blocked and event not in ("none", "complaint")),
        "safety_flag": False,
        "affected": affected,
    }


_EVENT_RANK = {"none": 0, "other": 1, "complaint": 2, "form_deadline": 3, "fee_deadline": 4, "admit_card": 5,
               "exam": 6, "harassment": 7, "medical": 8, "safety": 9}


def merge_facts(*sources: dict[str, Any] | None) -> UrgencyFacts:
    """Union of urgency evidence: highest-ranked event type, smallest minutes_to_event, any blocked/safety flag."""
    event, mins, blocked, safety, affected = "none", None, False, False, None
    for s in sources:
        if not s:
            continue
        e = s.get("event_type") or "none"
        if e not in _EVENT_RANK:
            e = "other"
        if _EVENT_RANK[e] > _EVENT_RANK[event]:
            event = e
        m = s.get("minutes_to_event")
        if isinstance(m, (int, float)) and m >= 0:
            mins = int(m) if mins is None else min(mins, int(m))
        blocked = blocked or bool(s.get("blocked"))
        safety = safety or bool(s.get("safety_flag"))
        affected = s.get("affected") or affected
    if affected not in ("self", "ward", "other"):
        affected = None
    return UrgencyFacts(event_type=event, minutes_to_event=mins, blocked=blocked, safety_flag=safety, affected=affected)


# --------------------------------------------------------------------------- slots for urgent / complaint flows
_FILLER = frozenset(
    "roll number no ka hai hain mera meri paper subject ke liye hai ye yeh mein me is the my and aur "
    "number nahi pata please plz sir madam ji haan yaar yaad bhool gaya gayi dont don know idk sorry hmm umm um ok okay no na nope abhi kuch".split()
)


def extract_paper(text: str, roll: str | None) -> str | None:
    """Best-effort paper/subject name: words after 'paper'/'subject' keywords, else the words left once the
    roll number and filler are removed (<= 6 words)."""
    t = re.sub(r"\d[\d\s\-]{8,}\d", " ", text or "")
    m = re.search(r"(?:paper|subject|exam)\s*(?:is|hai|:)?\s*([A-Za-z][A-Za-z0-9 &\-\.]{2,40})", t, re.I)
    if m:
        cand = m.group(1).strip(" .,-")
        cand = re.split(r"\s+(?:hai|hain|ka|ki|ke|mein|me|room|roll)\b", cand, flags=re.I)[0].strip()
        if cand:
            return cand[:40]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-\.]*", t) if w.lower() not in _FILLER]
    if 1 <= len(words) <= 6:
        return " ".join(words)[:40]
    return None


_LOCATION = re.compile(
    r"((?:block|blk|wing|floor|tower|hall|building|lab|room|rm|kamra|कमरा|ब्लॉक)\s*[-#:]?\s*[A-Za-z0-9]{1,4}"
    r"(?:\s*[,/&]?\s*(?:room|rm|kamra|no\.?)?\s*[-#:]?\s*\d{1,4})?)", re.I)


def extract_location(text: str) -> str | None:
    """'Block B room 214' style location from free text."""
    t = redact(text or "")      # "roll no 2101" must not look like a room code
    m = _LOCATION.search(t)
    if not m:
        m2 = re.search(r"\b([A-Za-z]{1,2}[-\s]?\d{2,4})\b", t)
        return m2.group(1) if m2 else None
    return re.sub(r"\s+", " ", t[m.start(): m.end() + 0]).strip(" ,.")


_CATEGORY_WORDS = {
    "wifi": ("wifi", "wi fi", "internet", "network", "वाईफाई"),
    "mess": ("mess", "food", "khana", "canteen"),
    "water": ("water", "pani", "geyser", "पानी"),
    "electricity": ("bijli", "electricity", "light", "power", "fan", "ac", "बिजली"),
    "cleanliness": ("clean", "cleaning", "safai", "washroom", "toilet", "dirty"),
    "transport": ("bus", "transport"),
    "room": ("room", "kamra", "furniture", "bed", "lift"),
    "exam": ("exam", "result", "marks", "admit"),
    "fees": ("fee", "fees", "payment", "refund"),
}


def infer_category(text: str) -> str | None:
    t = " " + normalize(text) + " "
    for cat, words in _CATEGORY_WORDS.items():
        if any((" " + normalize(w) + " ") in t for w in words):
            return cat
    return None
