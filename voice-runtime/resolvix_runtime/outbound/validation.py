"""Destination checks: E.164, allowed countries, emergency/short codes, India calling hours. Pure functions (no I/O)."""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from .models import OutboundError, forbidden, validation_error

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
IST = timezone(timedelta(hours=5, minutes=30))          # India has no DST, so no tz database is needed

# National numbers (what follows the country code) that are emergency, helpline or short codes. Never dialled, in any country.
EMERGENCY_NATIONAL = frozenset({
    "100", "101", "102", "108", "112", "1091", "1098", "181", "1930", "1070", "1077", "1033", "1073", "1090",   # India
    "911", "999", "000", "15", "17", "18", "110", "113", "114", "115", "118", "119", "122", "123", "190", "191", "192", "193", "199",
})
MIN_NATIONAL_DIGITS = 6                                  # anything shorter is a short code, never a subscriber number
INDIA_MOBILE = re.compile(r"^\+91[6-9]\d{9}$")           # +91 is restricted to mobile numbers in this MVP


@dataclass(frozen=True)
class Destination:
    e164: str            # full number: lives in memory only, never stored or logged
    masked: str          # +91*******210
    hash: str            # keyed hash, for duplicate / per-destination limits
    country_code: str    # "91"


def mask_number(e164: str) -> str:
    return e164[:3] + "*" * max(0, len(e164) - 6) + e164[-3:]


def number_hash(e164: str, key: bytes) -> str:
    return hmac.new(key, e164.encode(), hashlib.sha256).hexdigest()[:32]


def parse_destination(raw: str, allowed_country_codes: tuple[str, ...], hash_key: bytes, blocked: tuple[str, ...] = ()) -> Destination:
    """E.164 only (no spaces, dashes, trunk prefixes). Raises validation_error (shape) or forbidden (policy)."""
    number = (raw or "").strip()
    if not E164.fullmatch(number):
        raise validation_error("The number must be in E.164 format, for example +919876543210.", field="to")
    digits = number[1:]
    code = next((c for c in sorted(allowed_country_codes, key=len, reverse=True) if digits.startswith(c)), None)
    if code is None:
        raise forbidden("This destination country is not approved for outbound calls.", reason="country_not_allowed")
    national = digits[len(code):]
    if national in EMERGENCY_NATIONAL or len(national) < MIN_NATIONAL_DIGITS or any(national.startswith(e) and len(national) <= 4 for e in EMERGENCY_NATIONAL):
        raise forbidden("Emergency, helpline and short-code numbers can never be called.", reason="emergency_number")
    if code == "91" and len(national) != 10:
        raise validation_error("Indian numbers have ten digits after +91, for example +919876543210.", field="to")
    if code == "91" and not INDIA_MOBILE.fullmatch(number):
        raise forbidden("Only Indian mobile numbers (+91 followed by 6-9 and nine digits) are approved.", reason="number_type_not_allowed")
    if number in blocked:
        raise forbidden("This number cannot be called (it is the caller ID or on the block list).", reason="blocked_number")
    return Destination(number, mask_number(number), number_hash(number, hash_key), code)


def parse_hours(window: str) -> tuple[time, time]:
    start, end = window.split("-")
    h1, m1 = (int(x) for x in start.split(":"))
    h2, m2 = (int(x) for x in end.split(":"))
    return time(h1, m1), time(h2, m2)


def check_calling_hours(dest: Destination, now_epoch: float, window: str, enforce: bool) -> None:
    """Indian numbers: 09:00-21:00 IST (TCCCPR calling hours also apply to automated/IVR calls). Not caller-selectable."""
    if not enforce or dest.country_code != "91":
        return
    start, end = parse_hours(window)
    local = datetime.fromtimestamp(now_epoch, IST).time()
    if not (start <= local < end):
        raise forbidden(f"Calls to India are only allowed between {window} IST.", reason="outside_calling_hours")
