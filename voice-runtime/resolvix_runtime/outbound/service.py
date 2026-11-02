"""The outbound calling service: validation, limits, idempotency, lifecycle, callbacks, cancellation, watchdog, audit.

Honesty rules baked in:
  * a status the carrier has not reported is never invented (no `ringing`);
  * closing our WebSocket is NOT treated as hanging up the phone call: a cancel on a live call records `cancel_requested` and the call
    only becomes terminal on a carrier report, a confirmed provider hang-up, or the local watchdog (recorded as hangup_confirmed=false);
  * the full phone number lives in memory only while a call is live.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..config import RuntimeSettings
from ..ops_client import OpsClient
from ..sessions import get_session_registry
from ..voice_api import VoiceWebApi
from .models import (TERMINAL, CallStatus, OutboundCallRequest, OutboundError, conflict, forbidden, not_found, rate_limited)
from .provider import DisabledExotelProvider, MockProvider, PlaceCall, ProviderError, TelephonyProvider
from .settings import OutboundSettings
from .speech import AsrAdapter, MockAsr, MockTts, TtsAdapter
from .store import OutboundStore
from .validation import check_calling_hours, parse_destination

log = logging.getLogger("resolvix_runtime.outbound")
ROLES_ALLOWED = ("admin", "staff")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


class OutboundService:
    def __init__(self, runtime_settings: RuntimeSettings, settings: OutboundSettings, ops_client: OpsClient,
                 provider: TelephonyProvider | None = None, asr_factory: Callable[[str], AsrAdapter] | None = None,
                 tts_factory: Callable[[str], TtsAdapter] | None = None, clock: Callable[[], float] = time.time,
                 store: OutboundStore | None = None) -> None:
        self.rs, self.cfg, self.ops, self.clock = runtime_settings, settings, ops_client, clock
        # Only the mock provider is ever active in this build; any other configured value yields a stub that refuses every request.
        self.provider: TelephonyProvider = provider or (MockProvider() if settings.provider == "mock" else DisabledExotelProvider())
        self.asr_factory = asr_factory or (lambda call_id: MockAsr())
        self.tts_factory = tts_factory or (lambda call_id: MockTts())
        self._store = store
        self._hash_key = hashlib.sha256(b"outbound-v1|" + runtime_settings.service_key.encode()).digest()
        self._create_lock = asyncio.Lock()
        self._numbers: dict[str, str] = {}                  # call_id -> full E.164, live calls only, never persisted
        self.bridges: dict[str, Any] = {}                   # call_id -> live BridgeSession
        self.voice = VoiceWebApi(runtime_settings, ops_client)      # reused only for its per-turn Brain/executor/finalize logic
        self._watchdog: asyncio.Task | None = None

    # ------------------------------------------------------------------ plumbing
    @property
    def store(self) -> OutboundStore:
        if self._store is None:
            self._store = OutboundStore(self.cfg.db_path)
        return self._store

    def audit(self, call_id: str | None, event: str, actor: str | None = None, **detail: Any) -> None:
        self.store.audit(self.clock(), call_id, event, actor, **detail)

    def require_enabled(self) -> None:
        if not self.cfg.enabled:
            raise forbidden("Outbound calling is disabled on this server (OUTBOUND_ENABLED).", reason="outbound_disabled")

    @staticmethod
    def public(row: dict[str, Any]) -> dict[str, Any]:
        """The only shape that leaves the service: no number, no token hashes, no consent note."""
        return {
            "call_id": row["call_id"], "status": row["status"], "destination_masked": row["dest_masked"], "requested_by": row["requested_by"],
            "lang": row["lang"], "created_at": _iso(row["created_at"]), "dialing_at": _iso(row["dialing_at"]),
            "answered_at": _iso(row["answered_at"]), "connected_at": _iso(row["connected_at"]), "ended_at": _iso(row["ended_at"]),
            "duration_s": row["duration_s"], "outcome": row["outcome"], "failure_reason": row["failure_reason"],
            "provider": {"name": row["provider"], "sid": row["provider_sid"]},
            "cancel_requested": bool(row["cancel_requested_at"]), "hangup_confirmed": bool(row["hangup_confirmed"]), "cost": None,
        }

    # ------------------------------------------------------------------ create
    async def create_call(self, user: str, role: str, idempotency_key: str, req: OutboundCallRequest) -> tuple[dict[str, Any], bool]:
        """Returns (public call, created). created=False is an idempotent replay of the same request."""
        self.require_enabled()
        if role not in ROLES_ALLOWED:
            raise forbidden("Your role may not place outbound calls.", reason="role_not_allowed")
        fp = _sha(json.dumps({"to": req.to.strip(), "lang": req.lang, "basis": req.consent.basis}, sort_keys=True))
        async with self._create_lock:
            existing = self.store.get_by_idempotency(user, idempotency_key)
            if existing:
                if existing["request_fp"] != fp:
                    raise conflict("This Idempotency-Key was already used for a different request.", reason="idempotency_key_reuse")
                return self.public(existing), False
            now = self.clock()
            dest = parse_destination(req.to, self.cfg.allowed_country_codes, self._hash_key,
                                     blocked=tuple(n for n in (*self.cfg.blocked_numbers, self.cfg.caller_id) if n))
            if not req.consent.confirmed:
                raise forbidden("The recipient's consent must be confirmed before a call is placed.", reason="consent_required")
            check_calling_hours(dest, now, self.cfg.hours_window, self.cfg.enforce_hours)
            self._check_limits(user, dest.hash, now)
            call_id = f"call-out-{uuid.uuid4().hex[:12]}"
            ws_token, cb_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            self.store.insert({
                "call_id": call_id, "requested_by": user, "requester_role": role, "idempotency_key": idempotency_key, "request_fp": fp,
                "dest_masked": dest.masked, "dest_hash": dest.hash, "country_code": dest.country_code, "lang": req.lang,
                "consent_basis": req.consent.basis, "consent_note": req.consent.note, "consent_at": now, "status": CallStatus.queued.value,
                "created_at": now, "provider": self.provider.name, "ws_token_hash": _sha(ws_token), "ws_token_expires": now + self.cfg.token_ttl_s,
                "cb_token_hash": _sha(cb_token),
            })
            self.audit(call_id, "created", user, dest=dest.masked, country=dest.country_code, consent_basis=req.consent.basis, lang=req.lang)
            self._numbers[call_id] = dest.e164
            ws_base = self.cfg.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
            place = PlaceCall(call_id=call_id, to=dest.e164, caller_id=self.cfg.caller_id, stream_url=f"{ws_base}/ws/exotel/{ws_token}",
                              status_callback_url=f"{self.cfg.public_base_url}/v1/webhooks/exotel/{cb_token}",
                              time_limit_s=self.cfg.max_call_seconds, ring_timeout_s=self.cfg.ring_timeout_s, custom_field=call_id)
        try:
            provided = await self.provider.place_call(place)
        except ProviderError as exc:
            self._numbers.pop(call_id, None)
            self.store.set_status(call_id, CallStatus.failed, self.clock(), failure_reason=f"provider_error: {str(exc)[:100]}")
            self.audit(call_id, "provider_error", user, retryable=exc.retryable, error=str(exc)[:100])
            return self.public(self.store.get(call_id)), True
        self.store.set_status(call_id, CallStatus.dialing, self.clock(), provider_sid=provided.sid)
        self.audit(call_id, "dialing", user, provider_sid=provided.sid, provider=self.provider.name)
        return self.public(self.store.get(call_id)), True

    def _check_limits(self, user: str, dest_hash: str, now: float) -> None:
        c, s = self.cfg, self.store
        if s.active_for_destination(dest_hash, now - c.duplicate_window_s):
            raise conflict("A call to this number is already active or was just requested.", reason="duplicate_call")
        if s.count_active() >= c.max_concurrent:
            raise rate_limited("Too many calls are active right now.", 15, reason="max_concurrent")
        if s.count_user_since(user, now - 3600) >= c.per_user_hour:
            raise rate_limited("Hourly call limit reached for this user.", 600, reason="per_user_hour")
        if s.count_user_since(user, now - 86400) >= c.per_user_day:
            raise rate_limited("Daily call limit reached for this user.", 3600, reason="per_user_day")
        if s.count_destination_since(dest_hash, now - 86400) >= c.per_destination_day:
            raise rate_limited("This number has already been called the maximum number of times today.", 3600, reason="per_destination_day")
        reserved = (s.count_active() + 1) * c.max_call_seconds
        if s.seconds_used_since(now - 86400) + reserved > c.daily_seconds_cap:
            raise rate_limited("The daily outbound call-time cap would be exceeded.", 3600, reason="daily_cap")

    # ------------------------------------------------------------------ read / cancel
    def get(self, call_id: str) -> dict[str, Any]:
        self.require_enabled()
        row = self.store.get(call_id)
        if row is None:
            raise not_found()
        return self.public(row)

    def history(self, limit: int, before: float | None, status: str | None, user: str | None = None) -> list[dict[str, Any]]:
        self.require_enabled()
        if status and status not in {s.value for s in CallStatus}:
            raise OutboundError("validation_error", "Unknown status filter.", 422, details={"field": "status"})
        return [self.public(r) for r in self.store.history(max(1, min(limit, 100)), before, status, user)]

    async def cancel(self, call_id: str, actor: str) -> dict[str, Any]:
        """Idempotent. A call that was never dialled is cancelled at once; a live call is only *asked* to end (see module docstring)."""
        self.require_enabled()
        row = self.store.get(call_id)
        if row is None:
            raise not_found()
        status = CallStatus(row["status"])
        if status in TERMINAL:
            return self.public(row)
        if status == CallStatus.queued:
            self.store.set_status(call_id, CallStatus.cancelled, self.clock(), failure_reason="cancelled_before_dial", hangup_confirmed=1)
            self.audit(call_id, "cancelled", actor, before_dial=True)
            self._cleanup(call_id)
            return self.public(self.store.get(call_id))
        if not row["cancel_requested_at"]:
            self.store.update(call_id, cancel_requested_at=self.clock(), cancel_reason=f"requested_by:{actor}")
            self.audit(call_id, "cancel_requested", actor)
        confirmed = False
        try:
            result = await self.provider.hangup(row["provider_sid"]) if row["provider_sid"] else None
            confirmed = bool(result and result.confirmed)
            self.audit(call_id, "hangup_attempt", actor, supported=bool(result and result.supported), confirmed=confirmed)
        except ProviderError as exc:
            self.audit(call_id, "hangup_error", actor, error=str(exc)[:100])
        await self.close_bridge(call_id, "cancelled")
        if confirmed:
            await self.finish(call_id, CallStatus.cancelled, "cancelled_by_request", hangup_confirmed=1)
        return self.public(self.store.get(call_id))

    # ------------------------------------------------------------------ provider callbacks
    @staticmethod
    def map_callback(payload: dict[str, Any]) -> CallStatus | None:
        event, status = str(payload.get("EventType", "")).lower(), str(payload.get("Status", "")).lower()
        if event == "answered" or status == "in-progress":
            return CallStatus.answered
        return {"completed": CallStatus.ended, "busy": CallStatus.busy, "failed": CallStatus.failed, "no-answer": CallStatus.no_answer,
                "canceled": CallStatus.cancelled, "cancelled": CallStatus.cancelled}.get(status)

    async def handle_callback(self, cb_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Authenticated by the unguessable per-call token in the URL (Exotel callbacks carry no signature) AND the provider Sid match."""
        row = self.store.find_by_token("cb", _sha(cb_token))
        if row is None:
            raise not_found("Unknown callback.")
        sid = str(payload.get("CallSid", ""))
        if not row["provider_sid"] or sid != row["provider_sid"]:
            self.audit(row["call_id"], "callback_rejected", None, reason="sid_mismatch")
            raise forbidden("The callback does not match this call.", reason="sid_mismatch")
        new = self.map_callback(payload)
        if new is None:
            self.audit(row["call_id"], "callback_ignored", None, status=str(payload.get("Status", ""))[:20])
            return {"ok": True, "applied": False}
        duration = payload.get("ConversationDuration")
        extra: dict[str, Any] = {}
        if isinstance(duration, (int, str)) and str(duration).isdigit() and new in TERMINAL:
            extra["duration_s"] = int(duration)
        if new in TERMINAL:
            applied = await self.finish(row["call_id"], new, f"carrier:{payload.get('Status', '')}"[:60], hangup_confirmed=1, **extra)
        else:
            applied = self.store.set_status(row["call_id"], new, self.clock())
            if not applied:
                self.audit(row["call_id"], "callback_ignored", None, status=new.value, reason="illegal_or_duplicate")
        if applied:
            self.audit(row["call_id"], "callback_applied", None, status=new.value)
        return {"ok": True, "applied": applied}

    # ------------------------------------------------------------------ websocket support
    def lookup_ws(self, token: str) -> dict[str, Any] | None:
        row = self.store.find_by_token("ws", _sha(token))
        if row is None or row["ws_started"] or self.clock() > (row["ws_token_expires"] or 0) or CallStatus(row["status"]) in TERMINAL:
            return None
        return row

    def number_for(self, call_id: str) -> str | None:
        return self._numbers.get(call_id)

    async def close_bridge(self, call_id: str, reason: str) -> None:
        bridge = self.bridges.get(call_id)
        if bridge is not None:
            await bridge.close(reason)

    # ------------------------------------------------------------------ terminal handling
    async def finish(self, call_id: str, status: CallStatus, reason: str, **fields: Any) -> bool:
        """Move a call to a terminal status once; then close its bridge, end the Brain session and drop the in-memory number."""
        if "failure_reason" not in fields and status != CallStatus.ended:
            fields["failure_reason"] = reason
        if status == CallStatus.ended:
            fields.setdefault("outcome", reason)
        applied = self.store.set_status(call_id, status, self.clock(), **fields)
        if applied:
            self.audit(call_id, "terminal", None, status=status.value, reason=reason)
        await self.close_bridge(call_id, reason)
        await self._end_brain_session(call_id, reason)
        self._cleanup(call_id)
        return applied

    async def _end_brain_session(self, call_id: str, reason: str) -> None:
        session = get_session_registry().get(call_id)
        if session is not None and not session.finalized:
            try:
                info = await self.voice.finalize(session, reason=f"outbound:{reason}"[:40])
                self.store.update(call_id, ops_call_id=call_id, outcome=info.get("outcome") or reason)
            except Exception as exc:                          # the call record must close even if ops/brain misbehave
                log.warning("outbound %s finalize failed: %s", call_id, type(exc).__name__)

    def _cleanup(self, call_id: str) -> None:
        self._numbers.pop(call_id, None)
        self.bridges.pop(call_id, None)

    # ------------------------------------------------------------------ watchdog
    async def tick(self, now: float | None = None) -> int:
        """One watchdog pass: stuck queued/dialing calls, the hard maximum duration, unconfirmed cancels. Returns changes made."""
        now = self.clock() if now is None else now
        changed = 0
        c = self.cfg
        for row in self.store.active_calls():
            cid, status = row["call_id"], CallStatus(row["status"])
            if status == CallStatus.queued and now - row["created_at"] > 30:
                changed += await self.finish(cid, CallStatus.failed, "stuck_queued")
            elif status == CallStatus.dialing and now - (row["dialing_at"] or row["created_at"]) > c.ring_timeout_s + 20:
                changed += await self.finish(cid, CallStatus.no_answer, "no_carrier_report_after_ring_timeout")
            elif status in (CallStatus.answered, CallStatus.connected):
                started = row["connected_at"] or row["answered_at"] or row["created_at"]
                if now - started > c.max_call_seconds and not row["cancel_requested_at"]:
                    self.store.update(cid, cancel_requested_at=now, cancel_reason="max_duration")
                    self.audit(cid, "max_duration_reached", None, limit_s=c.max_call_seconds)
                    await self.close_bridge(cid, "max_duration")
                    try:
                        await self.provider.hangup(row["provider_sid"])
                    except ProviderError:
                        pass
                    changed += 1
            if row["cancel_requested_at"] and status in (CallStatus.answered, CallStatus.connected, CallStatus.dialing) \
                    and now - row["cancel_requested_at"] > 15:
                reason = "max_duration_enforced_locally" if row["cancel_reason"] == "max_duration" else "cancel_unconfirmed_by_carrier"
                final = CallStatus.ended if row["cancel_reason"] == "max_duration" else CallStatus.cancelled
                changed += await self.finish(cid, final, reason, hangup_confirmed=0)
        return changed

    async def start(self) -> None:
        if self.cfg.enabled and self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            self._watchdog = None
        for call_id in list(self.bridges):
            await self.close_bridge(call_id, "shutdown")
        if self._store is not None:
            self._store.close()
            self._store = None

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                await self.tick()
                self.store.purge_consent_notes(self.clock() - self.cfg.consent_retention_days * 86400)
            except Exception as exc:
                log.warning("outbound watchdog error: %s", type(exc).__name__)
