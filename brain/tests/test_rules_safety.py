"""Urgency rule table (first match wins, boundaries) and the pre-LLM safety / human-request lexicon."""
from __future__ import annotations

import time

import pytest
import yaml

from conftest import ROOT, Caller, actions, done, kinds
from resolvix_brain.rules import RuleTable
from resolvix_brain.safety import Lexicon
from resolvix_brain.urgency import extract_facts, merge_facts, minutes_to_event

RULES = ROOT / "contracts" / "config" / "urgency_rules.yaml"
table = RuleTable.from_file(RULES)
lex = Lexicon.from_rules_file(RULES)


def ev(**kw):
    base = {"event_type": "none", "minutes_to_event": None, "blocked": False, "safety_flag": False, "affected": None,
            "intent": "faq", "tier": "confident"}
    base.update(kw)
    return table.evaluate(base)


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize("mins,expected", [(59, "R-EXAM-60"), (60, "R-EXAM-60"), (61, "R-ROUTINE"), (0, "R-EXAM-60")])
def test_exam_boundary(mins, expected):
    assert ev(event_type="exam", minutes_to_event=mins, blocked=True).rule_id == expected


@pytest.mark.parametrize("mins,expected", [(1439, "R-DEADLINE-24H"), (1440, "R-DEADLINE-24H"), (1441, "R-ROUTINE")])
@pytest.mark.parametrize("etype", ["fee_deadline", "form_deadline", "admit_card"])
def test_deadline_boundary(mins, expected, etype):
    assert ev(event_type=etype, minutes_to_event=mins, blocked=True).rule_id == expected


def test_not_blocked_is_not_urgent():
    assert ev(event_type="exam", minutes_to_event=15, blocked=False).rule_id == "R-ROUTINE"
    assert ev(event_type="fee_deadline", minutes_to_event=100, blocked=False).rule_id == "R-ROUTINE"


def test_missing_minutes_never_satisfies_a_numeric_rule():
    assert ev(event_type="exam", minutes_to_event=None, blocked=True).rule_id == "R-ROUTINE"


def test_first_match_wins_and_priorities():
    # safety beats everything, even with exam facts and a human request
    r = ev(event_type="exam", minutes_to_event=5, blocked=True, safety_flag=True, intent="human_request", tier="escalate")
    assert (r.rule_id, r.priority, r.ticket_type, r.department) == ("R-SAFETY", "P1", "EMERGENCY", "emergency")
    assert ev(event_type="exam", minutes_to_event=5, blocked=True, tier="escalate").rule_id == "R-EXAM-60"
    assert ev(event_type="complaint").rule_id == "R-COMPLAINT"
    assert ev(intent="human_request", tier="escalate").rule_id == "R-HUMAN"       # before R-UNANSWERED
    r = ev(tier="escalate")
    assert (r.rule_id, r.priority, r.ticket_type, r.action) == ("R-UNANSWERED", "P3", "KNOWLEDGE_GAP", "ticket_and_gap")
    r = ev()
    assert (r.rule_id, r.priority, r.ticket_type, r.action) == ("R-ROUTINE", "P4", None, "none")


def test_tone_is_not_an_input():
    # facts have no emotion field; an angry routine question is still P4
    assert ev(event_type="none").priority == "P4"


def test_rule_file_shape():
    data = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    ids = [r["id"] for r in data["rules"]]
    assert ids[0] == "R-SAFETY" and ids[-1] == "R-ROUTINE" and "R-FALLBACK" in ids and len(ids) == len(set(ids))
    assert table.rule("R-FALLBACK").ticket_type == "SYSTEM_FAILURE"


# ------------------------------------------------------------------ deterministic fact extraction
@pytest.mark.parametrize("text,mins", [
    ("Exam 15 minute mein hai", 15), ("exam in 30 minutes", 30), ("exam ek ghante mein hai", 60),
    ("paper 2 hours baad", 120), ("aadha ghanta bacha hai", 30), ("exam kab hai", None)])
def test_minutes_to_event(text, mins):
    assert minutes_to_event(text) == mins


def test_extract_exam_urgency_facts():
    f = merge_facts(extract_facts("Exam 15 minute mein hai, room nahi mil raha!"))
    assert (f.event_type, f.minutes_to_event, f.blocked, f.affected) == ("exam", 15, True, None)
    f = merge_facts(extract_facts("mera exam 15 minute mein hai aur hall nahi mil raha"))
    assert f.event_type == "exam" and f.blocked and f.affected == "self"
    assert extract_facts("exam kab hai")["event_type"] == "none"          # no time, not blocked: routine
    assert extract_facts("hostel ka wifi nahi chal raha")["event_type"] == "complaint"
    assert extract_facts("hostel mein wifi password kya hai")["event_type"] == "none"


def test_timed_exam_outranks_admit_card_wording_in_hinglish():
    for text in ("mera data structures ka exam 15 minute mein hai aur hall ticket download nahi ho raha",
                 "mera exam 20 minute mein hai aur mujhe admit card nahi mil raha"):
        f = merge_facts(extract_facts(text))
        assert (f.event_type, f.blocked) == ("exam", True) and f.minutes_to_event in (15, 20)
    assert extract_facts("exam ka admit card nahi mil raha")["event_type"] == "admit_card"       # no time: still an admit-card issue
    assert extract_facts("admit card download nahi ho raha")["event_type"] == "admit_card"


def test_merge_never_lowers_safety():
    f = merge_facts({"event_type": "exam", "minutes_to_event": 90}, {"event_type": "medical", "safety_flag": True,
                                                                     "minutes_to_event": 20})
    assert f.event_type == "medical" and f.safety_flag and f.minutes_to_event == 20


# ------------------------------------------------------------------ safety lexicon
def _all_terms():
    data = yaml.safe_load(RULES.read_text(encoding="utf-8"))["safety_lexicon"]
    return [(cat, t) for cat, terms in data.items() for t in terms]


@pytest.mark.parametrize("cat,term", _all_terms())
def test_every_lexicon_term_triggers(cat, term):
    hit = lex.safety_hit(f"please help, {term} ho gaya hai")
    assert hit is not None


@pytest.mark.parametrize("text", [
    "Hostel mein ek ladka behosh ho gaya hai", "Meri dost ko ragging kar rahe hain", "मेरा दोस्त बेहोश हो गया",
    "वो मुझे धमकी दे रहे हैं", "I want to commit suicide", "someone is bleeding badly", "ambulance bulao jaldi",
    "mujhe saans nahi aa rahi", "there was an accident near the gate", "वह आत्महत्या की बात कर रहा है",
    "कैंपस में मारपीट हो रही है", "he has a knife", "Heart Attack aaya hai!", "BEHOSH!!!"])
def test_emergency_phrases_hit(text):
    assert lex.safety_hit(text) is not None, text


@pytest.mark.parametrize("text", [
    "hostel ki fees kitni hai", "What time does the hostel gate close?", "exam kab se hain", "mess ka time kya hai",
    "B.Tech second year fees ki last date kya hai?", "mujhe admit card chahiye", "library kab khulti hai",
    "main behoshi ke baare mein nahi, fees ke baare mein puch raha hoon".replace("behoshi", "behoshiyan"),
    "firefox browser nahi chal raha"])
def test_normal_sentences_do_not_hit(text):
    assert lex.safety_hit(text) is None, text


def test_safety_hit_details():
    h = lex.safety_hit("Hostel mein ek ladka behosh ho gaya hai")
    assert (h.category, h.event_type, h.affected) == ("medical", "medical", "other")
    assert lex.safety_hit("mujhe ragging ka dar hai").affected == "self"


def test_safety_is_fast():
    t = time.perf_counter()
    for _ in range(200):
        lex.safety_hit("mera exam 15 minute mein hai aur hall nahi mil raha, please jaldi batao kahan jaana hai")
    per_call_ms = (time.perf_counter() - t) / 200 * 1000
    assert per_call_ms < 5, per_call_ms


@pytest.mark.parametrize("text,dtmf,expected", [
    ("Mujhe kisi insaan se baat karni hai", None, True), ("I want to talk to a person", None, True),
    ("can I speak to a real person", None, True), ("operator please", None, True), ("", "0", True),
    ("मुझे किसी से बात करनी है", None, True), ("what is the human resources department", None, False),
    ("hostel fees kitni hai", None, False), ("", "5", False)])
def test_human_request_lexicon(text, dtmf, expected):
    assert lex.human_request(text, dtmf) is expected


# ------------------------------------------------------------------ end to end: safety never calls the LLM
async def test_safety_path_never_calls_llm_even_inside_a_flow(brain):
    c = Caller(brain, "c-safe", "+919000000003")
    await c.say("Hostel ka wifi do din se nahi chal raha")                 # opens a complaint flow
    assert brain.sessions.get("c-safe").pending.name == "complaint"
    calls_before = len(brain.llm_client.calls)
    ev_ = await c.say("ek ladki behosh ho gayi hai")                        # safety overrides the pending flow
    assert done(ev_).path == "safety" and done(ev_).rule_id == "R-SAFETY"
    assert kinds(ev_)[:3] == ["meta", "speech", "create_ticket"]
    assert len(brain.llm_client.calls) == calls_before
    assert brain.sessions.get("c-safe").pending is None
    t = actions(ev_, "create_ticket")[0]
    assert (t.ticket.type, t.ticket.priority, t.ticket.department) == ("EMERGENCY", "P1", "emergency")


async def test_llm_extracted_safety_flag_also_takes_the_emergency_plan(brain):
    brain.llm_client.queue.append(
        '<meta>{"tier":"escalate","kb_ids":[],"lang":"hinglish","intent":"other","urgency_facts":'
        '{"event_type":"medical","minutes_to_event":null,"blocked":true,"safety_flag":true,"affected":"other"},"needs":[]}</meta>\nok')
    c = Caller(brain, "c-llm-safe", "+919000000003")
    ev_ = await c.say("mere bhai ki halat bahut kharab hai kuch bol nahi pa raha")
    assert done(ev_).rule_id == "R-SAFETY" and done(ev_).path == "safety"
    assert [a.kind for a in actions(ev_)] == ["create_ticket", "send_sms", "send_sms", "transfer"]


# ------------------------------------------------------------------ R-FALLBACK ordering (first match wins)
def test_fallback_rule_sits_above_unanswered():
    ids = [r["id"] for r in table.rules]
    assert ids.index("R-FALLBACK") < ids.index("R-UNANSWERED")
    assert ids.index("R-SAFETY") < ids.index("R-FALLBACK")           # safety still beats everything


def test_fallback_with_escalate_tier_is_not_a_knowledge_gap():
    r = ev(intent="system_failure", tier="escalate")
    assert (r.rule_id, r.ticket_type, r.action) == ("R-FALLBACK", "SYSTEM_FAILURE", "ticket_and_callback")


@pytest.mark.parametrize("tier", ["confident", "partial", "clarify", "chitchat"])
def test_fallback_with_non_escalate_tier(tier):
    assert ev(intent="system_failure", tier=tier).rule_id == "R-FALLBACK"


@pytest.mark.parametrize("intent", ["faq", "other", "complaint"])
def test_genuine_unanswered_question_is_still_a_knowledge_gap(intent):
    r = ev(intent=intent, tier="escalate")
    assert (r.rule_id, r.ticket_type, r.action) == ("R-UNANSWERED", "KNOWLEDGE_GAP", "ticket_and_gap")


def test_fallback_does_not_outrank_emergency_or_human_request():
    assert ev(intent="system_failure", tier="escalate", safety_flag=True).rule_id == "R-SAFETY"
    assert ev(event_type="exam", minutes_to_event=10, blocked=True, intent="system_failure",
              tier="escalate").rule_id == "R-EXAM-60"
    assert ev(intent="human_request", tier="escalate").rule_id == "R-HUMAN"


# ------------------------------------------------------------------ ambiguous safety words (safety_exclusions)
@pytest.mark.parametrize("text", [
    "Fire exit location?", "Fire safety drill schedule?", "Sexual harassment committee contact?",
    "where is the fire extinguisher in the lab", "fire drill kab hai", "anti ragging affidavit chahiye",
    "anti ragging committee ka number do", "sexual harassment policy ka pdf chahiye", "attack on titan club kab milta hai",
    "cyber attack ke baare mein seminar hai kya", "threat modeling course ki fees", "fight club ka timing kya hai",
    "faint signal aa raha hai wifi pe", "garmi se mar jaunga hostel mein fan nahi chal raha"])
def test_ordinary_questions_with_ambiguous_words_do_not_hit(text):
    assert lex.safety_hit(text) is None, text


@pytest.mark.parametrize("text,category", [
    ("There is a fire in the lab", "violence"), ("fire lag gayi hai hostel mein", "violence"),
    ("fire exit ke paas aag lag gayi hai", "violence"),                    # exclusion covers 'fire exit' only
    ("fire safety drill ke time ek ladka behosh ho gaya", "medical"),
    ("fire exit pe fire lag gayi", "violence"),                           # second, unexcluded 'fire'
    ("two boys have a fight in the canteen", "violence"), ("someone is going to attack me", "violence"),
    ("he is threatening me", "harassment"), ("mujhe dhamki mil rahi hai", "harassment"),
    ("mujhe sexual harassment hua hai", "harassment"),
    ("sexual harassment committee ko batana hai, ek senior mujhe harass kar raha hai", "harassment"),
    ("anti ragging committee ko batao, mujhe ragging kar rahe hain", "harassment"),
    ("she may faint, please send help", "medical"), ("ek ladki faint ho gayi", "medical"),
    ("mar jaunga", "self_harm"), ("main mar jaunga", "self_harm"), ("ab main mar jaungi", "self_harm"),
    ("fail hua toh mar jaunga, main suicide kar lunga", "self_harm"),
    ("Fail hua toh mar jaunga, main suicide kar lunga.", "self_harm"),
    ("Main sach mein apni jaan lena chahta hoon", "self_harm"), ("main sach mein apni jaan le lunga", "self_harm"),
    ("मैं सच में अपनी जान लेना चाहता हूं", "self_harm"),
    ("Fail hua toh mar jaunga", "self_harm"), ("fail ho gaya toh mar jaungi", "self_harm"),   # exam context never excludes it
    ("फेल हुआ तो मर जाऊंगा", "self_harm"),
    ("fail hua toh mar jaunga aur khud ko maar dunga", "self_harm"),
    ("kill myself", "self_harm"), ("मैं मर जाऊंगा", "self_harm")])
def test_clear_emergencies_still_hit_even_next_to_an_excluded_phrase(text, category):
    h = lex.safety_hit(text)
    assert h is not None and h.category == category, (text, h)


def test_exclusions_never_hide_an_emergency_term_appended_to_them():
    excl = yaml.safe_load(RULES.read_text(encoding="utf-8"))["safety_exclusions"]
    assert excl and all(isinstance(e, str) and e.strip() for e in excl)
    for e in excl:
        assert lex.safety_hit(e) is None, e                                     # the phrase alone is ordinary
        assert lex.safety_hit(f"{e} aur ek ladka behosh ho gaya hai") is not None, e
        assert lex.safety_hit(f"{e}, i will kill myself") is not None, e


def test_safety_exclusions_are_specific_phrases():
    excl = [e.lower() for e in yaml.safe_load(RULES.read_text(encoding="utf-8"))["safety_exclusions"]]
    for bare in ("fire", "fight", "attack", "threat", "sexual", "faint", "mar jaunga", "mar jaungi"):
        assert bare not in excl, bare
    assert all(len(e.split()) >= 2 for e in excl)


async def test_fire_exit_question_does_not_take_the_emergency_path(brain):
    c = Caller(brain, "c-fire-exit", "+919000000003")
    ev_ = await c.say("Fire exit location?")
    assert done(ev_).path != "safety" and done(ev_).rule_id != "R-SAFETY"
    assert not actions(ev_, "create_ticket") or actions(ev_, "create_ticket")[0].ticket.type != "EMERGENCY"


async def test_real_fire_still_takes_the_emergency_path(brain):
    c = Caller(brain, "c-real-fire", "+919000000003")
    ev_ = await c.say("fire exit ke paas aag lag gayi hai")
    assert done(ev_).path == "safety" and done(ev_).rule_id == "R-SAFETY"


# ------------------------------------------------------------------ human-request exclusions
@pytest.mark.parametrize("text", [
    "I need a human", "Connect me to a real person", "Mujhe kisi insaan se baat karni hai", "talk to a human please",
    "human se baat karao", "can I speak to someone", "operator please", "press 0", "agent se baat karni hai",
    "I need a human, not human resources", "human anatomy nahi, mujhe kisi insaan se baat karni hai"])
def test_human_assistance_triggers(text):
    assert lex.human_request(text) is True, text


@pytest.mark.parametrize("text", [
    "Human anatomy", "Human body", "Human physiology", "human anatomy syllabus kya hai", "human body ka lab kab hai",
    "human physiology practical timing", "human resources department", "human rights club", "humanities block kahan hai",
    "human computer interaction elective", "human values course"])
def test_human_topics_do_not_trigger_human_assistance(text):
    assert lex.human_request(text) is False, text


def test_dtmf_zero_always_requests_a_human():
    assert lex.human_request("human anatomy", "0") is True


# ------------------------------------------------------------------ exam anxiety vs explicit self-harm intent
@pytest.mark.parametrize("text", [
    "exam mein fail ho gaya, bahut tension hai", "fail hua toh papa daantenge", "fail hone ka bahut dar lag raha hai",
    "mujhe exam ka bahut stress hai, backlog clear kaise hoga", "result kharab aaya, ab kya karun", "fail ho gaya toh kya re-exam hota hai",
    "exam anxiety ke liye counsellor ka number do", "paper bura gaya, mummy gussa hongi"])
def test_ordinary_exam_anxiety_is_not_an_emergency(text):
    assert lex.safety_hit(text) is None, text


@pytest.mark.parametrize("text", [
    "Fail hua toh mar jaunga, main suicide kar lunga.", "Main sach mein apni jaan lena chahta hoon",
    "fail ho gaya, ab main khud ko khatam kar dunga", "exam ke baad main kill myself karunga",
    "exam mein fail hua, jeena nahi chahta, marna chahta hoon", "फेल हो गया, अब आत्महत्या कर लूंगा",
    "Fail hua toh mar jaunga"])
def test_exam_context_never_cancels_explicit_self_harm(text):
    h = lex.safety_hit(text)
    assert h is not None and h.category == "self_harm" and h.event_type == "safety", (text, h)


def test_no_exclusion_mentions_exams_or_self_harm_phrases():
    excl = [e.lower() for e in yaml.safe_load(RULES.read_text(encoding="utf-8"))["safety_exclusions"]]
    assert not [e for e in excl if "fail" in e.split() or "exam" in e.split() or "फेल" in e.split()]
    sh = [t for t in yaml.safe_load(RULES.read_text(encoding="utf-8"))["safety_lexicon"]["self_harm"] if " " in t or t.isascii()]
    # only hunger/heat/laughter idioms may cover a self-harm term
    for e in excl:
        if lex.safety_hit(e + " ") is None and any(t.lower() in e for t in sh):
            assert e.split()[:2] in (["bhookh", "se"], ["garmi", "se"], ["hans", "hans"]), e


async def test_fail_hua_toh_mar_jaunga_takes_the_emergency_path(brain):
    c = Caller(brain, "c-exam-sh", "+919000000003")
    calls = len(brain.llm_client.calls)
    ev_ = await c.say("Fail hua toh mar jaunga, main suicide kar lunga.")
    assert done(ev_).path == "safety" and done(ev_).rule_id == "R-SAFETY" and len(brain.llm_client.calls) == calls
    assert actions(ev_, "create_ticket")[0].ticket.priority == "P1"
