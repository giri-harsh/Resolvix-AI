"""Outbound calling: statuses, the state machine, request models and the error type.

Status lifecycle (see integration/OUTBOUND_API.md):
    queued -> dialing -> answered -> connected -> ended
    any non-terminal -> failed | no_answer | busy | cancelled
`ringing` is deliberately absent: Exotel's callback payload has no ringing event (docs disagree), so it is never invented.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CallStatus(str, Enum):
    queued = "queued"
    dialing = "dialing"
    answered = "answered"
    connected = "connected"
    ended = "ended"
    failed = "failed"
    no_answer = "no_answer"
    busy = "busy"
    cancelled = "cancelled"


TERMINAL = frozenset({CallStatus.ended, CallStatus.failed, CallStatus.no_answer, CallStatus.busy, CallStatus.cancelled})

_FAIL_STATES = {CallStatus.failed, CallStatus.no_answer, CallStatus.busy, CallStatus.cancelled}
ALLOWED: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.queued: frozenset({CallStatus.dialing} | _FAIL_STATES),
    CallStatus.dialing: frozenset({CallStatus.answered, CallStatus.connected, CallStatus.ended} | _FAIL_STATES),
    CallStatus.answered: frozenset({CallStatus.connected, CallStatus.ended} | _FAIL_STATES),
    CallStatus.connected: frozenset({CallStatus.ended} | _FAIL_STATES),
    **{s: frozenset() for s in TERMINAL},
}


def can_transition(old: CallStatus, new: CallStatus) -> bool:
    return new in ALLOWED[old]


class OutboundError(Exception):
    """An error that maps to the contract's ErrorEnvelope (code must be one of ErrorBody's codes)."""

    def __init__(self, code: str, message: str, http_status: int, retryable: bool = False, details: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.http_status, self.retryable, self.details = code, message, http_status, retryable, details or {}


def validation_error(message: str, **details: str) -> OutboundError:
    return OutboundError("validation_error", message, 422, details=details)


def forbidden(message: str, **details: str) -> OutboundError:
    """HTTP 403. The frozen contract's ErrorBody has no `forbidden` code, so the envelope code is `unauthorized`; the machine-readable
    reason is in details.reason (consent_required, country_not_allowed, emergency_number, outside_calling_hours, ...)."""
    return OutboundError("unauthorized", message, 403, details=details)


def conflict(message: str, **details: str) -> OutboundError:
    return OutboundError("conflict", message, 409, details=details)


def rate_limited(message: str, retry_after_s: int = 60, **details: str) -> OutboundError:
    return OutboundError("rate_limited", message, 429, retryable=True, details={"retry_after_s": str(retry_after_s), **details})


def not_found(message: str = "Unknown call.") -> OutboundError:
    return OutboundError("not_found", message, 404)


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool
    basis: Literal["customer_requested", "existing_relationship", "opt_in"]
    note: str = Field(default="", max_length=200)


class OutboundCallRequest(BaseModel):
    """Caller-supplied fields only. There is NO `purpose` / category field: the compliance treatment (calling hours, limits)
    is decided by the server, so a caller cannot label a call to dodge a rule (unknown fields are rejected)."""

    model_config = ConfigDict(extra="forbid")
    to: str = Field(min_length=3, max_length=20)
    lang: Literal["hi", "en", "hinglish"] = "hinglish"
    consent: Consent
