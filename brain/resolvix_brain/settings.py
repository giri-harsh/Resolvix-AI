"""Brain settings, loaded from environment variables (see ``.env.example``).

Relative paths in ``KB_SOURCE``, ``DEPARTMENTS_FILE`` and ``URGENCY_RULES_FILE`` are resolved against the
``brain/`` directory (the parent of this package), so the CLI works from any working directory.
Secrets (``GROQ_API_KEY``) are never printed: ``repr`` and ``safe_dict`` mask them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable

BRAIN_DIR = Path(__file__).resolve().parents[1]
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_IST_FALLBACK = timezone(timedelta(hours=5, minutes=30), "IST")


def get_tz(name: str) -> tzinfo:
    """Return the named zone; India has no DST so a fixed +05:30 is a safe fallback when tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - only without tzdata
        return _IST_FALLBACK


def _resolve(p: str) -> str:
    path = Path(p)
    return str(path if path.is_absolute() else (BRAIN_DIR / path).resolve())


def _env_file_values() -> dict[str, str]:
    """Tiny .env reader (no extra dependency). Real environment variables win over the file."""
    out: dict[str, str] = {}
    f = BRAIN_DIR / ".env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class BrainSettings:
    llm_mode: str = "live"                      # live | mock
    groq_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"
    llm_fallback_model: str = "openai/gpt-oss-20b"
    llm_reasoning_effort: str = "low"
    llm_max_tokens: int = 300
    llm_temperature: float = 0.2
    llm_ttft_timeout_s: float = 3.0
    llm_total_timeout_s: float = 6.0
    llm_retry_if_elapsed_under_s: float = 1.5
    llm_bench_after_empty: int = 2              # answer-less responses in a row before a model is tried second (0 = never)
    llm_bench_seconds: float = 300.0            # how long it is tried second
    groq_base_url: str = "https://api.groq.com/openai/v1"
    kb_source: str = "file:../contracts/kb/sample_kb.jsonl"
    kb_export_service_key: str = ""             # sent as X-Service-Key when the KB source is an http(s) URL
    kb_token_budget: int = 90000
    faq_tau_hit: float = 0.90
    faq_tau_soft: float = 0.80
    faq_index_all: bool = False
    embed_mode: str = "fastembed"               # fastembed | lexical
    embed_model: str = "intfloat/multilingual-e5-small"
    embed_cache_dir: str = "./.cache/fastembed"
    departments_file: str = "../contracts/config/departments.yaml"
    urgency_rules_file: str = "../contracts/config/urgency_rules.yaml"
    timezone: str = "Asia/Kolkata"
    session_ttl_s: int = 7200
    warmup_on_reload: bool = True
    # Test seam: freeze "today" for KB validity filtering (the per-turn clock is TurnRequest.received_at).
    today_override: date | None = None
    clock: Callable[[], datetime] | None = field(default=None, repr=False)

    def __repr__(self) -> str:  # never leak the API key
        d = self.safe_dict()
        return "BrainSettings(" + ", ".join(f"{k}={v!r}" for k, v in d.items()) + ")"

    def safe_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "clock"}
        d["groq_api_key"] = "***" if self.groq_api_key else ""
        d["kb_export_service_key"] = "***" if self.kb_export_service_key else ""
        return d

    @property
    def tz(self) -> tzinfo:
        return get_tz(self.timezone)

    def today(self) -> date:
        if self.today_override:
            return self.today_override
        return datetime.now(self.tz).date()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BrainSettings":
        src = {**_env_file_values(), **(os.environ if env is None else env)}

        def s(key: str, default: str) -> str:
            return src.get(key, default)

        def b(key: str, default: bool) -> bool:
            return src.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

        d = cls()
        return cls(
            llm_mode=s("LLM_MODE", d.llm_mode).lower(),
            groq_api_key=s("GROQ_API_KEY", ""),
            llm_model=s("LLM_MODEL", d.llm_model),
            llm_fallback_model=s("LLM_FALLBACK_MODEL", d.llm_fallback_model),
            llm_reasoning_effort=s("LLM_REASONING_EFFORT", d.llm_reasoning_effort),
            llm_max_tokens=int(s("LLM_MAX_TOKENS", str(d.llm_max_tokens))),
            llm_temperature=float(s("LLM_TEMPERATURE", str(d.llm_temperature))),
            llm_ttft_timeout_s=float(s("LLM_TTFT_TIMEOUT_S", str(d.llm_ttft_timeout_s))),
            llm_total_timeout_s=float(s("LLM_TOTAL_TIMEOUT_S", str(d.llm_total_timeout_s))),
            llm_retry_if_elapsed_under_s=float(s("LLM_RETRY_IF_ELAPSED_UNDER_S", str(d.llm_retry_if_elapsed_under_s))),
            llm_bench_after_empty=int(s("LLM_BENCH_AFTER_EMPTY", str(d.llm_bench_after_empty))),
            llm_bench_seconds=float(s("LLM_BENCH_SECONDS", str(d.llm_bench_seconds))),
            groq_base_url=s("GROQ_BASE_URL", d.groq_base_url).rstrip("/"),
            kb_source=s("KB_SOURCE", d.kb_source),
            kb_export_service_key=s("KB_EXPORT_SERVICE_KEY", "").strip(),
            kb_token_budget=int(s("KB_TOKEN_BUDGET", str(d.kb_token_budget))),
            faq_tau_hit=float(s("FAQ_TAU_HIT", str(d.faq_tau_hit))),
            faq_tau_soft=float(s("FAQ_TAU_SOFT", str(d.faq_tau_soft))),
            faq_index_all=b("FAQ_INDEX_ALL", d.faq_index_all),
            embed_mode=s("EMBED_MODE", d.embed_mode).lower(),
            embed_model=s("EMBED_MODEL", d.embed_model),
            embed_cache_dir=_resolve(s("EMBED_CACHE_DIR", d.embed_cache_dir)),
            departments_file=_resolve(s("DEPARTMENTS_FILE", d.departments_file)),
            urgency_rules_file=_resolve(s("URGENCY_RULES_FILE", d.urgency_rules_file)),
            timezone=s("TIMEZONE", d.timezone),
            session_ttl_s=int(s("SESSION_TTL_S", str(d.session_ttl_s))),
            warmup_on_reload=b("WARMUP_ON_RELOAD", d.warmup_on_reload),
        )

    def default_kb_source(self):
        """Parse ``KB_SOURCE`` (``file:<path>`` or ``url:<http-url>``) into a contract ``KBSource``."""
        from resolvix_contracts.models import KBSource

        kind, _, loc = self.kb_source.partition(":")
        if kind == "url":
            return KBSource(kind="url", location=loc)
        if kind == "file":
            return KBSource(kind="file", location=_resolve(loc))
        return KBSource(kind="file", location=_resolve(self.kb_source))
