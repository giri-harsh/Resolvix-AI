"""The top-40 eval runner's matching rules (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import top40_eval_runner as r  # noqa: E402


def test_question_id_field_q_is_read_and_languages_stay_one_question():
    rows = [{"q": "Q07", "lang": "en", "utterance": "a", "expected_entry_id": "FEE-1", "must_contain": "25,000"},
            {"q": "Q07", "lang": "hi", "utterance": "b", "expected_entry_id": "FEE-1", "must_contain": "25,000"}]
    items = r.expand(rows)
    assert [(i["qid"], i["lang"]) for i in items] == [("Q07", "en"), ("Q07", "hi")]


def test_must_contain_matches_the_displayed_form_and_the_spoken_form():
    assert r.contains("The fee is 1 lakh 42 thousand 156 rupees for the first year.", "1,42,156")
    assert r.contains("late fine is 200 rupees", "Rs 200")
    assert r.contains("Gates close at 10 pm", "10 PM|11 pm")
    assert not r.contains("The fee is 1 lakh 40 thousand rupees", "1,42,156")      # a wrong figure never matches
    assert not r.contains("", "anything")


def test_utterance_map_form_is_expanded_per_language():
    items = r.expand([{"id": "Q1", "utterances": {"en": "x", "hinglish": "y"}, "expected_entry_id": "E-1", "must_contain": ["a", "b"]}])
    assert [i["lang"] for i in items] == ["en", "hinglish"] and items[0]["must"] == ["a", "b"]
