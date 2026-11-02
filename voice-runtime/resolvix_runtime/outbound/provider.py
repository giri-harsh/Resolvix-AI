"""Telephony provider abstraction. Only MockProvider is ever active in this build; the real Exotel provider is a disabled stub.

Nothing here performs network I/O. What Exotel's Connect request will look like is captured by build_connect_form (pure function)
so it can be reviewed and tested without calling anything; the parts the docs leave open are flagged UNVERIFIED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class PlaceCall:
    call_id: str
    to: str                      # full E.164, in memory only
    caller_id: str
    stream_url: str              # wss://<public host>/ws/exotel/<token>
    status_callback_url: str     # https://<public host>/v1/webhooks/exotel/<token>
    time_limit_s: int
    ring_timeout_s: int
    custom_field: str


@dataclass(frozen=True)
class ProviderCall:
    sid: str
    status: str = "queued"


@dataclass(frozen=True)
class HangupResult:
    supported: bool              # False = the provider has no documented way to end this call
    confirmed: bool = False      # True only when the provider confirmed the call is over


class TelephonyProvider(Protocol):
    name: str

    async def place_call(self, req: PlaceCall) -> ProviderCall: ...

    async def hangup(self, sid: str) -> HangupResult: ...


@dataclass
class MockProvider:
    """In-memory provider for tests and offline development. Deterministic, never touches the network."""

    name: str = "mock"
    hangup_supported: bool = False       # mirrors the documented reality: no hang-up API found
    hangup_confirms: bool = False
    fail_with: ProviderError | None = None
    placed: list[PlaceCall] = field(default_factory=list)
    hangups: list[str] = field(default_factory=list)
    _n: int = 0

    async def place_call(self, req: PlaceCall) -> ProviderCall:
        if self.fail_with is not None:
            raise self.fail_with
        self._n += 1
        self.placed.append(req)
        return ProviderCall(sid=f"MOCKSID{self._n:04d}")

    async def hangup(self, sid: str) -> HangupResult:
        self.hangups.append(sid)
        return HangupResult(supported=self.hangup_supported, confirmed=self.hangup_supported and self.hangup_confirms)


class DisabledExotelProvider:
    """The real Exotel provider is intentionally not implemented in this build: any use is an error, so no request can leave the machine."""

    name = "exotel"

    async def place_call(self, req: PlaceCall) -> ProviderCall:
        raise ProviderError("The real Exotel provider is disabled in this build (mock-only phase).")

    async def hangup(self, sid: str) -> HangupResult:
        raise ProviderError("The real Exotel provider is disabled in this build (mock-only phase).")


def build_connect_form(req: PlaceCall) -> dict[str, str]:
    """The form body planned for POST /v1/Accounts/{sid}/Calls/connect (Exotel 'Connect Voice AI'). Pure, no credentials.

    Documented as required: From, To, CallerId, StreamUrl, StreamType=bidirectional. UNVERIFIED (ask Exotel): which party `To` must be
    for a voice-AI leg (a bot has no number), whether `StatusCallbackEvents` may include `ringing`, and the callback content type.
    """
    return {
        "From": req.to,
        "To": req.caller_id,                       # UNVERIFIED
        "CallerId": req.caller_id,
        "StreamUrl": req.stream_url,
        "StreamType": "bidirectional",
        "TimeLimit": str(req.time_limit_s),
        "TimeOut": str(req.ring_timeout_s),
        "StatusCallback": req.status_callback_url,
        "StatusCallbackEvents": "answered,terminal",
        "StatusCallbackContentType": "application/json",
        "CustomField": req.custom_field,
    }
