"""Outbound calling settings, read from the process environment (os.getenv), like the rest of the runtime.

Canonical configuration source: integration/.env, which integration/run_all.ps1 loads into the environment of every service it starts.
voice-runtime/.env is NOT read by any code (the runtime has no dotenv loader), so values only placed there have no effect.
This mock-only build never reads EXOTEL_API_KEY / EXOTEL_API_TOKEN / EXOTEL_ACCOUNT_SID.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[3] / "integration" / ".run" / "outbound.db"      # integration/.run is git-ignored


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


@dataclass
class OutboundSettings:
    enabled: bool = False                       # off unless OUTBOUND_ENABLED=true
    provider: str = "mock"                      # the only supported value in this build
    db_path: str = str(DEFAULT_DB)
    public_base_url: str = "http://localhost:8000"
    caller_id: str = ""                         # EXOTEL_CALLER_ID (the ExoPhone); also never callable as a destination
    max_call_seconds: int = 180
    ring_timeout_s: int = 30
    max_concurrent: int = 3
    per_user_hour: int = 10
    per_user_day: int = 30
    per_destination_day: int = 2
    daily_seconds_cap: int = 7200               # total conversation seconds per day (reserved worst case counts)
    allowed_country_codes: tuple[str, ...] = ("91",)
    enforce_hours: bool = True                  # operator-level only: never a request field
    hours_window: str = "09:00-21:00"
    token_ttl_s: int = 180
    duplicate_window_s: int = 60
    consent_retention_days: int = 365
    blocked_numbers: tuple[str, ...] = field(default_factory=tuple)
    chunk_size: int = 3200

    @classmethod
    def from_env(cls) -> "OutboundSettings":
        g = os.getenv
        return cls(
            enabled=_flag("OUTBOUND_ENABLED", False),
            provider=g("OUTBOUND_PROVIDER", "mock").strip().lower(),
            db_path=g("OUTBOUND_DB_PATH", str(DEFAULT_DB)),
            public_base_url=(g("OUTBOUND_PUBLIC_BASE_URL") or g("PUBLIC_BASE_URL", "http://localhost:8000")).rstrip("/"),
            caller_id=g("EXOTEL_CALLER_ID", "").strip(),
            max_call_seconds=int(g("OUTBOUND_MAX_CALL_SECONDS", "180")),
            ring_timeout_s=int(g("OUTBOUND_RING_TIMEOUT_S", "30")),
            max_concurrent=int(g("OUTBOUND_MAX_CONCURRENT", "3")),
            per_user_hour=int(g("OUTBOUND_PER_USER_HOUR", "10")),
            per_user_day=int(g("OUTBOUND_PER_USER_DAY", "30")),
            per_destination_day=int(g("OUTBOUND_PER_DESTINATION_DAY", "2")),
            daily_seconds_cap=int(g("OUTBOUND_DAILY_SECONDS_CAP", "7200")),
            allowed_country_codes=tuple(c.strip().lstrip("+") for c in g("OUTBOUND_ALLOWED_COUNTRY_CODES", "91").split(",") if c.strip()),
            enforce_hours=_flag("OUTBOUND_ENFORCE_HOURS", True),
            hours_window=g("OUTBOUND_HOURS_WINDOW", "09:00-21:00"),
            token_ttl_s=int(g("OUTBOUND_TOKEN_TTL_S", "180")),
            duplicate_window_s=int(g("OUTBOUND_DUPLICATE_WINDOW_S", "60")),
            consent_retention_days=int(g("OUTBOUND_CONSENT_RETENTION_DAYS", "365")),
            blocked_numbers=tuple(n.strip() for n in g("OUTBOUND_BLOCKED_NUMBERS", "").split(",") if n.strip()),
        )
