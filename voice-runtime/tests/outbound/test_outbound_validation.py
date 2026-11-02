"""Destination rules: E.164, allowed countries, emergency/short codes, caller ID, calling hours. Pure functions, no I/O."""
from __future__ import annotations

import pytest

from resolvix_runtime.outbound.audio import chunk_for_exotel
from resolvix_runtime.outbound.models import CallStatus, OutboundError, can_transition
from resolvix_runtime.outbound.validation import check_calling_hours, mask_number, parse_destination

KEY = b"k" * 32
CODES = ("91",)


def test_valid_indian_mobile_is_accepted_and_masked_and_hashed():
    d = parse_destination("+919876543210", CODES, KEY)
    assert d.e164 == "+919876543210" and d.country_code == "91"
    assert d.masked == "+91*******210" and "9876543" not in d.masked
    assert len(d.hash) == 32 and d.hash != parse_destination("+919876543211", CODES, KEY).hash
    assert d.hash != parse_destination("+919876543210", CODES, b"other-key" * 4).hash          # keyed: not a plain hash of the number


@pytest.mark.parametrize("raw", ["", "9876543210", "919876543210", "+91 98765 43210", "+91-9876543210", "0919876543210", "+0919876543210",
                                 "+91987654321", "+9198765432100000", "tel:+919876543210", "+91abcdefghij", "+", "++919876543210", " +91 "])
def test_malformed_numbers_are_validation_errors(raw):
    with pytest.raises(OutboundError) as exc:
        parse_destination(raw, CODES, KEY)
    assert exc.value.code == "validation_error" and exc.value.http_status == 422


@pytest.mark.parametrize("raw", ["+14155550123", "+442071234567", "+971501234567", "+65812345678"])
def test_countries_that_are_not_approved_are_forbidden(raw):
    with pytest.raises(OutboundError) as exc:
        parse_destination(raw, CODES, KEY)
    assert exc.value.http_status == 403 and exc.value.details["reason"] == "country_not_allowed"


def test_international_destination_is_allowed_only_when_the_operator_lists_its_country_code():
    assert parse_destination("+14155550123", ("91", "1"), KEY).country_code == "1"
    assert parse_destination("+971501234567", ("91", "971"), KEY).country_code == "971"


@pytest.mark.parametrize("raw", ["+911234567890", "+915123456789", "+918765", "+91100", "+91112", "+91108", "+91102", "+91101", "+911091", "+911098", "+91181"])
def test_only_indian_mobile_numbers_pass_and_emergency_or_short_codes_never_do(raw):
    with pytest.raises(OutboundError) as exc:
        parse_destination(raw, CODES, KEY)
    assert exc.value.http_status in (403, 422)                      # shape error or policy refusal: either way it is never dialled


@pytest.mark.parametrize("national", ["100", "101", "102", "108", "112", "1091", "1098", "181", "911", "999"])
def test_emergency_numbers_are_blocked_even_when_a_country_would_allow_short_numbers(national):
    with pytest.raises(OutboundError) as exc:
        parse_destination("+91" + national, CODES, KEY)
    assert exc.value.http_status in (403, 422)


def test_caller_id_and_block_list_cannot_be_called():
    with pytest.raises(OutboundError) as exc:
        parse_destination("+919876543210", CODES, KEY, blocked=("+919876543210",))
    assert exc.value.details["reason"] == "blocked_number"


def test_calling_hours_apply_to_india_and_cannot_be_chosen_by_the_caller(clock):
    d = parse_destination("+919876543210", CODES, KEY)
    check_calling_hours(d, clock.t, "09:00-21:00", True)                       # noon IST
    for hour, minute in ((8, 59), (21, 0), (23, 30), (3, 0)):
        clock.set_ist(hour, minute)
        with pytest.raises(OutboundError) as exc:
            check_calling_hours(d, clock.t, "09:00-21:00", True)
        assert exc.value.details["reason"] == "outside_calling_hours"
    clock.set_ist(9, 0)
    check_calling_hours(d, clock.t, "09:00-21:00", True)                       # the window start is inclusive
    clock.set_ist(3, 0)
    check_calling_hours(d, clock.t, "09:00-21:00", False)                      # only an operator setting can switch it off


def test_calling_hours_rule_is_for_india_only(clock):
    clock.set_ist(3, 0)
    d = parse_destination("+14155550123", ("1",), KEY)
    check_calling_hours(d, clock.t, "09:00-21:00", True)


def test_mask_number_hides_the_middle():
    assert mask_number("+919876543210") == "+91*******210"


def test_state_machine_transitions():
    legal = [("queued", "dialing"), ("queued", "cancelled"), ("dialing", "answered"), ("dialing", "connected"), ("answered", "connected"),
             ("connected", "ended"), ("dialing", "no_answer"), ("dialing", "busy"), ("answered", "failed"), ("connected", "cancelled")]
    illegal = [("queued", "connected"), ("queued", "answered"), ("answered", "dialing"), ("connected", "answered"), ("ended", "failed"),
               ("failed", "dialing"), ("busy", "ended"), ("cancelled", "connected"), ("connected", "dialing"), ("queued", "ended")]
    for a, b in legal:
        assert can_transition(CallStatus(a), CallStatus(b)), (a, b)
    for a, b in illegal:
        assert not can_transition(CallStatus(a), CallStatus(b)), (a, b)
    assert "ringing" not in {s.value for s in CallStatus}


def test_audio_chunks_follow_the_documented_size_rules():
    pcm = b"\x01\x00" * 5000                                          # 10000 bytes
    chunks = chunk_for_exotel(pcm, 3200)
    assert all(len(c) % 320 == 0 and 3200 <= len(c) <= 100000 for c in chunks) and sum(len(c) for c in chunks) >= 10000
    assert chunk_for_exotel(b"", 3200) == []
    with pytest.raises(ValueError):
        chunk_for_exotel(pcm, 3000)
    with pytest.raises(ValueError):
        chunk_for_exotel(pcm, 3200 + 1)
