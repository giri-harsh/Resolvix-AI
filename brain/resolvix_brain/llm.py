"""LLM integration: Groq OpenAI-compatible client, deterministic mock client, META parsing, retry policy.

Policy (PRD 5.2 step 6): buffer the whole answer (<= ~300 tokens) and validate before anything is spoken.
Time-to-first-token timeout and total timeout are enforced; on a transport error or an unparseable answer we
retry **once on the fallback model, only if the elapsed time is under ``llm_retry_if_elapsed_under_s``**; after
that the caller gets the safe fallback (ticket + "we will call you back"). The API key is only ever placed in the
Authorization header and is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from resolvix_contracts.models import Lang, Tier, UrgencyFacts

from .settings import BrainSettings

log = logging.getLogger("resolvix_brain.llm")


# --------------------------------------------------------------------------- errors / responses
class LLMError(Exception):
    """Transport/API failure. ``kind`` is one of timeout | http | empty | truncated | config.

    ``empty``: the model finished (or the stream ended) without any answer text, e.g. Groq's gpt-oss returning only a
    reasoning trace. ``truncated``: it ran out of ``max_tokens`` before writing any answer text."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind


class LLMTimeout(LLMError):
    def __init__(self, message: str = "timeout"):
        super().__init__("timeout", message)


class LLMParseError(Exception):
    """The model answered but not in the required ``<meta>`` protocol."""


@dataclass
class LLMResponse:
    text: str
    model: str
    ttft_ms: int = 0
    total_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    finish_reason: str | None = None
    reasoning_tokens: int | None = None


class LLMClient(Protocol):
    async def complete(self, messages: list[dict[str, str]], *, model: str, max_tokens: int, temperature: float,
                       reasoning_effort: str, ttft_timeout: float, total_timeout: float,
                       require_content: bool = True) -> LLMResponse: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- META protocol
class LLMMeta(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tier: Tier
    kb_ids: list[str] = Field(default_factory=list)
    lang: Lang | None = None
    intent: Literal["faq", "human_request", "complaint", "callback_request", "other"] = "other"
    urgency_facts: dict[str, Any] = Field(default_factory=dict)
    needs: list[str] = Field(default_factory=list)

    @field_validator("tier", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("lang", mode="before")
    @classmethod
    def _lang_ok(cls, v):
        v = v.strip().lower() if isinstance(v, str) else v
        return v if v in ("hi", "en", "hinglish") else None

    @field_validator("intent", mode="before")
    @classmethod
    def _intent_ok(cls, v):
        v = v.strip().lower() if isinstance(v, str) else v
        return v if v in ("faq", "human_request", "complaint", "callback_request", "other") else "other"

    @field_validator("kb_ids", mode="before")
    @classmethod
    def _ids(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip().upper() for x in v if str(x).strip()]

    @field_validator("needs", mode="before")
    @classmethod
    def _needs(cls, v):
        return [str(x) for x in v] if isinstance(v, list) else []

    @field_validator("urgency_facts", mode="before")
    @classmethod
    def _facts(cls, v):
        if not isinstance(v, dict):
            return {}
        allowed = set(UrgencyFacts.model_fields)
        out = {k: v[k] for k in v if k in allowed}
        mins = out.get("minutes_to_event")
        if isinstance(mins, str) and mins.strip().lstrip("-").isdigit():
            out["minutes_to_event"] = int(mins)
        if not isinstance(out.get("minutes_to_event"), (int, type(None))) or isinstance(out.get("minutes_to_event"), bool):
            out["minutes_to_event"] = None
        return out


def parse_llm_output(text: str) -> tuple[LLMMeta, str]:
    """Split ``<meta>{json}</meta>`` from the spoken text. Raises ``LLMParseError`` on any deviation."""
    raw = (text or "").strip()
    start = raw.find("<meta>")
    if start < 0:
        raise LLMParseError("no <meta> block")
    body = raw[start + len("<meta>"):].lstrip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1] if "\n" in body else body[3:]
    brace = body.find("{")
    if brace < 0:
        raise LLMParseError("no JSON object in <meta>")
    try:
        obj, end = json.JSONDecoder().raw_decode(body[brace:])
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"invalid JSON in <meta>: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise LLMParseError("meta is not an object")
    try:
        meta = LLMMeta.model_validate(obj)
    except Exception as exc:
        raise LLMParseError(f"meta schema: {str(exc)[:120]}") from exc
    rest = body[brace + end:]
    rest = rest.replace("```", "", 1) if rest.lstrip().startswith("```") else rest
    close = rest.find("</meta>")
    spoken = rest[close + len("</meta>"):] if close >= 0 else rest
    spoken = spoken.replace("<meta>", "").replace("</meta>", "").strip()
    return meta, spoken


# --------------------------------------------------------------------------- Groq client
class GroqClient:
    def __init__(self, settings: BrainSettings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.groq_api_key:
            raise LLMError("config", "GROQ_API_KEY is not set")
        self._base = settings.groq_base_url
        self._key = settings.groq_api_key
        self._transport = transport  # test seam (httpx.MockTransport)
        self._client: httpx.AsyncClient | None = None

    def _http(self, ttft_timeout: float) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(connect=2.0, read=ttft_timeout, write=5.0, pool=2.0),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def complete(self, messages, *, model, max_tokens, temperature, reasoning_effort, ttft_timeout,
                       total_timeout, require_content: bool = True) -> LLMResponse:
        """Stream one completion. Only ``delta.content`` is answer text: ``delta.reasoning`` (gpt-oss thinking) is never
        returned. With ``require_content`` an answer-less stream raises ``LLMError`` (``truncated`` when the token
        budget ran out, otherwise ``empty``); the warm-up call passes False because it only wants the cache side effect."""
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort, "stream": True,
                "stream_options": {"include_usage": True}}
        t0 = time.perf_counter()
        parts: list[str] = []
        ttft_ms = 0
        usage: dict[str, Any] = {}
        finish: str | None = None
        reasoning_chars = 0
        try:
            async with asyncio.timeout(total_timeout):
                async with self._http(ttft_timeout).stream(
                        "POST", f"{self._base}/chat/completions", json=body,
                        headers={"Authorization": f"Bearer {self._key}"}) as r:
                    if r.status_code != 200:
                        detail = (await r.aread())[:200].decode("utf-8", "replace")
                        raise LLMError("http", f"status {r.status_code}: {detail}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage") or usage
                        for ch in chunk.get("choices") or []:
                            delta = ch.get("delta") or {}
                            piece = delta.get("content")
                            if (piece or delta.get("reasoning")) and not ttft_ms:
                                ttft_ms = int((time.perf_counter() - t0) * 1000)
                            if delta.get("reasoning"):
                                reasoning_chars += len(delta["reasoning"])
                            if piece:
                                parts.append(piece)
                            finish = ch.get("finish_reason") or finish
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise LLMTimeout(f"{type(exc).__name__} after {time.perf_counter() - t0:.1f}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError("http", type(exc).__name__) from exc
        text = "".join(parts)
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if not text.strip() and require_content:
            # metadata only, never text: enough to tell "ran out of budget" from "model answered nothing"
            raise LLMError("truncated" if finish == "length" else "empty",
                           f"no answer text (finish_reason={finish}, completion_tokens={usage.get('completion_tokens')}, "
                           f"reasoning_tokens={reasoning_tokens}, reasoning_chars={reasoning_chars})")
        details = usage.get("prompt_tokens_details") or {}
        return LLMResponse(text, model, ttft_ms, int((time.perf_counter() - t0) * 1000),
                           usage.get("prompt_tokens"), usage.get("completion_tokens"),
                           details.get("cached_tokens"), finish, reasoning_tokens)


# --------------------------------------------------------------------------- deterministic mock
_FACTS_NONE = {"event_type": "none", "minutes_to_event": None, "blocked": False, "safety_flag": False, "affected": None}


def _meta(tier: str, ids: list[str], lang: str, intent: str = "faq", facts: dict | None = None,
          needs: list[str] | None = None) -> str:
    return "<meta>" + json.dumps({"tier": tier, "kb_ids": ids, "lang": lang, "intent": intent,
                                  "urgency_facts": facts or _FACTS_NONE, "needs": needs or []}) + "</meta>\n"


class MockLLMClient:
    """Keyword-keyed canned answers covering every validator branch (no network).

    ``queue`` lets a test script exact responses: each item is a string (the raw model output), an ``LLMError``
    instance (raised), or a callable ``messages -> str``. When the queue is empty the keyword table is used.
    Trigger words in the *user* text: ``mock-timeout``, ``mock-invalid-json``, ``mock-bad-citation``,
    ``mock-ungrounded``, ``mock-hours-wrong``, ``mock-language``, ``mock-injection``, ``mock-clarify``.
    """

    def __init__(self) -> None:
        self.queue: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return None

    def _answer(self, user: str) -> str:
        u = user.lower()
        if "mock-timeout" in u or "admission" in u:
            raise LLMTimeout("mock timeout")
        if "mock-invalid-json" in u:
            return "<meta>{not json}</meta>\nHostel gate 10 baje band hota hai."
        if "mock-bad-citation" in u:
            return _meta("confident", ["ZZZ-999"], "en") + "Hostel gates close at 10 pm."
        if "mock-ungrounded" in u:
            return _meta("confident", ["FEE-002"], "en") + "A late fine of 250 rupees per day applies after the due date."
        if "mock-hours-wrong" in u:
            return _meta("confident", ["HST-003"], "en") + "Hostel gates close at 10 am on weekdays."
        if "mock-language" in u:
            return _meta("confident", ["HST-003"], "hi") + "Hostel gates close at 10 pm on weekdays."
        if "mock-injection" in u or "ignore all previous" in u:
            return _meta("escalate", [], "en", "other") + "I can only help with college information."
        if "mock-clarify" in u:
            return _meta("clarify", [], "hinglish", needs=["program"]) + "Aap kis course ke baare mein pooch rahe hain, B.Tech ya MBA?"
        if "gate" in u or "gates" in u:
            return _meta("confident", ["HST-003"], "en") + "Hostel gates close at 10 pm on weekdays and 11 pm on weekends, as per the Hostel Rules."
        if "re-evaluation" in u or "reevaluation" in u:
            return _meta("partial", ["EXM-004"], "hinglish") + (
                "Re-evaluation ka form exam result ke baad Exam Cell se milta hai, lekin is semester ki fees mere "
                "paas confirmed nahi hai. Kya main aapko Exam Cell se jod doon?")
        if "mess" in u and ("timing" in u or "time" in u or "kab" in u):
            return _meta("confident", ["HST-004"], "hinglish") + (
                "Mess ka time subah 7:30 se 9, dopahar 12:30 se 2, aur raat 7:30 se 9 baje tak hai, Hostel Rules ke mutabik.")
        if "paper kuch der" in u:
            return _meta("escalate", [], "hinglish", "other", {"event_type": "exam", "minutes_to_event": 15, "blocked": True,
                                                              "safety_flag": False, "affected": "self"}) + "Main aapko abhi staff se jodta hoon."
        if "saans" in u and "mock-llm-safety" in u:
            return _meta("escalate", [], "hinglish", "other", {**_FACTS_NONE, "event_type": "medical", "safety_flag": True}) + "Turant madad bhej raha hoon."
        if "insaan" in u or "human" in u or "mock-human" in u:
            return _meta("escalate", [], "hinglish", "human_request") + "Bilkul, main aapko staff se jodta hoon."
        if "complaint" in u or "shikayat" in u:
            return _meta("escalate", [], "hinglish", "complaint", {**_FACTS_NONE, "event_type": "complaint"}) + "Main aapki complaint darj karta hoon."
        if "thanks" in u or "dhanyavaad" in u or "shukriya" in u:
            return _meta("chitchat", [], "hinglish", "other") + "Aapka swagat hai."
        return _meta("escalate", [], "hinglish", "faq") + "Is baare mein mere paas pakki jaankari nahi hai."

    async def complete(self, messages, *, model, max_tokens, temperature, reasoning_effort, ttft_timeout,
                       total_timeout, require_content: bool = True) -> LLMResponse:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.calls.append({"model": model, "user": user, "messages": messages})
        await asyncio.sleep(0)  # a real await point: cancellation tests need one
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            text = item(messages) if callable(item) else item
        else:
            text = self._answer(user)
        return LLMResponse(text, model, ttft_ms=1, total_ms=2, prompt_tokens=None, completion_tokens=None,
                           cached_tokens=None, finish_reason="stop")


# --------------------------------------------------------------------------- service (retry policy, usage, health)
@dataclass
class LLMOutcome:
    meta: LLMMeta
    spoken: str
    response: LLMResponse
    flags: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    used_fallback_model: bool = False


class LLMUnavailable(Exception):
    def __init__(self, flags: list[str], elapsed_ms: int):
        super().__init__(",".join(flags))
        self.flags = flags
        self.elapsed_ms = elapsed_ms


@dataclass
class UsageTotals:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    failures: int = 0

    @property
    def cache_ratio(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


class LLMService:
    def __init__(self, client: LLMClient, settings: BrainSettings):
        self.client = client
        self.s = settings
        self.usage: dict[str, UsageTotals] = {}
        self.consecutive_failures = 0
        self.last_failure_at = 0.0
        self._empty_streak: dict[str, int] = {}      # model -> consecutive answer-less responses
        self._benched_until: dict[str, float] = {}   # model -> monotonic time until which it is tried second, not first

    # ---- model benching
    # Groq's gpt-oss-120b intermittently finishes with only a reasoning trace and no answer (measured: most calls on our
    # prompts, regardless of reasoning_effort or max_tokens, streaming or not). Each such call burns ~1 s before the
    # fallback model answers, so after ``llm_bench_after_empty`` empties in a row the model is tried second for
    # ``llm_bench_seconds``. A success resets the count; the model is never removed.
    def _is_benched(self, model: str) -> bool:
        return self._benched_until.get(model, 0.0) > time.monotonic()

    def _note_empty(self, model: str) -> None:
        n = self._empty_streak.get(model, 0) + 1
        self._empty_streak[model] = n
        if self.s.llm_bench_after_empty > 0 and n >= self.s.llm_bench_after_empty:
            self._benched_until[model] = time.monotonic() + self.s.llm_bench_seconds
            self._empty_streak[model] = 0
            log.warning("llm %s returned no answer %d times in a row: tried second for %.0fs", model, n, self.s.llm_bench_seconds)

    # ---- health
    @property
    def is_down(self) -> bool:
        return self.consecutive_failures >= 3 and (time.monotonic() - self.last_failure_at) < 60

    def _record(self, resp: LLMResponse | None, model: str) -> None:
        u = self.usage.setdefault(model, UsageTotals())
        if resp is None:
            u.failures += 1
            self.consecutive_failures += 1
            self.last_failure_at = time.monotonic()
            return
        self.consecutive_failures = 0
        u.calls += 1
        u.prompt_tokens += resp.prompt_tokens or 0
        u.completion_tokens += resp.completion_tokens or 0
        u.cached_tokens += resp.cached_tokens or 0

    async def _call(self, messages, model: str, deadline_left: float | None = None) -> LLMResponse:
        total = self.s.llm_total_timeout_s if deadline_left is None else max(0.5, min(self.s.llm_total_timeout_s, deadline_left))
        return await self.client.complete(
            messages, model=model, max_tokens=self.s.llm_max_tokens, temperature=self.s.llm_temperature,
            reasoning_effort=self.s.llm_reasoning_effort, ttft_timeout=self.s.llm_ttft_timeout_s, total_timeout=total)

    async def run(self, messages: list[dict[str, str]],
                  check: Callable[[LLMMeta, str], str | None] | None = None) -> LLMOutcome:
        """Primary model, then (time permitting) one retry on the fallback model.

        ``check(meta, spoken)`` may return a short reason (e.g. ``"language_mismatch"``) to request a retry for a
        soft failure; the retried answer is accepted as is. Raises ``LLMUnavailable`` when no usable answer exists.
        """
        t0 = time.perf_counter()
        flags: list[str] = []
        last_soft: LLMOutcome | None = None
        order = (self.s.llm_model, self.s.llm_fallback_model)
        if self.s.llm_model != self.s.llm_fallback_model and self._is_benched(self.s.llm_model):
            order = (self.s.llm_fallback_model, self.s.llm_model)
            flags.append("llm_primary_benched")
        retry_gate = self.s.llm_retry_if_elapsed_under_s
        for attempt, model in enumerate(order):
            elapsed = time.perf_counter() - t0
            if attempt == 1 and elapsed >= retry_gate:
                flags.append("llm_retry_skipped")
                break
            try:
                resp = await self._call(messages, model, None if attempt == 0 else self.s.llm_total_timeout_s - elapsed)
            except asyncio.CancelledError:
                raise
            except LLMTimeout:
                self._record(None, model)
                flags.append("llm_timeout" if attempt == 0 else "llm_retry_failed")
                continue
            except LLMError as exc:
                self._record(None, model)
                if exc.kind in ("empty", "truncated"):
                    # The provider answered, just without text, and quickly: the other model can still answer inside the
                    # total budget, so do not apply the short gate meant for slow primaries and unparseable answers.
                    retry_gate = max(retry_gate, self.s.llm_total_timeout_s - 2.0)
                if exc.kind == "empty":
                    self._note_empty(model)
                flags.append(f"llm_{exc.kind}" if attempt == 0 else "llm_retry_failed")
                log.warning("llm %s failed: %s", model, exc.kind)
                continue
            self._empty_streak[model] = 0
            self._record(resp, model)
            if resp.finish_reason == "length":
                flags.append("llm_truncated" if attempt == 0 else "llm_retry_failed")
                continue
            try:
                meta, spoken = parse_llm_output(resp.text)
            except LLMParseError as exc:
                flags.append("json_retry" if attempt == 0 else "llm_retry_failed")
                log.warning("llm output rejected (%s)", exc)
                continue
            outcome = LLMOutcome(meta, spoken, resp, list(flags), int((time.perf_counter() - t0) * 1000),
                                 model == self.s.llm_fallback_model != self.s.llm_model)
            reason = check(meta, spoken) if (check and attempt == 0) else None
            if reason and attempt == 0:
                flags.append(reason + "_retry")
                last_soft = outcome  # keep: used if the retry fails
                continue
            if last_soft is not None and attempt == 1:
                outcome.flags = list(flags)
            return outcome
        if last_soft is not None:
            last_soft.flags = list(flags) + ["retry_failed_kept_first"]
            return last_soft
        raise LLMUnavailable(flags, int((time.perf_counter() - t0) * 1000))

    async def warmup(self, prefix: str) -> dict[str, Any]:
        """One tiny call with the new prefix so the provider caches it (read ``cached_tokens`` on the next call)."""
        msgs = [{"role": "system", "content": prefix}, {"role": "user", "content": "ping"}]
        # max_tokens=8 is all spent on reasoning by gpt-oss (finish_reason=length, no answer text): that is fine here,
        # the call only has to make the provider cache the prefix, so an answer-less response is not an error.
        resp = await self.client.complete(msgs, model=self.s.llm_model, max_tokens=8, temperature=0.0,
                                          reasoning_effort=self.s.llm_reasoning_effort,
                                          ttft_timeout=self.s.llm_ttft_timeout_s * 3, total_timeout=self.s.llm_total_timeout_s * 3,
                                          require_content=False)
        return {"ttft_ms": resp.ttft_ms, "total_ms": resp.total_ms, "prompt_tokens": resp.prompt_tokens,
                "cached_tokens": resp.cached_tokens}


def create_llm(settings: BrainSettings) -> tuple[LLMClient, LLMService]:
    """Live Groq client, or the mock. A missing key in live mode yields a client that fails fast (safe fallback)."""
    if settings.llm_mode == "mock":
        client: LLMClient = MockLLMClient()
    else:
        try:
            client = GroqClient(settings)
        except LLMError:
            log.error("LLM_MODE=live but GROQ_API_KEY is missing: every LLM turn will use the safe fallback")
            client = _NoKeyClient()
    return client, LLMService(client, settings)


class _NoKeyClient:
    async def complete(self, *a, **k):
        raise LLMError("config", "GROQ_API_KEY is not set")

    async def aclose(self) -> None:
        return None
