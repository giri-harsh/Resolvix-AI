"""Browser voice chat API ("Plan B"): the HTTP API the voice-web page uses. Mounted at /api/voice by app.py.

The browser does speech-to-text and text-to-speech itself (Chrome); this API only moves TEXT between the page and the Brain and
keeps ops (calls, turns, tickets, SMS) consistent. Full contract: integration/VOICE_WEB_API.md (v1).

    POST /api/voice/calls                       start a call            -> greeting, call_id
    POST /api/voice/calls/{id}/turns            send what the caller said -> SSE stream (default) or one JSON reply (?stream=false)
    POST /api/voice/calls/{id}/cancel           barge-in: cancel the turn that is being spoken
    POST /api/voice/calls/{id}/end              end the call (also posts /v1/calls/{id}/end to ops); idempotent
    GET  /api/voice/config                      limits and timeouts the client should respect

Rules: a turn that is not interruptable (emergency, ticket creation) can never be cancelled, and it keeps running to the end (ticket,
alert, transfer) even if the browser disconnects (CR-8). Idle calls are ended by the server. Errors use the contract's ErrorEnvelope.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from resolvix_contracts import CONTRACT_VERSION
from resolvix_contracts.models import (
    CallChannel,
    CallContext,
    CallEnd,
    CallOutcome,
    CallStart,
    ErrorBody,
    ErrorEnvelope,
    TurnRequest,
)
from .brain_loader import get_brain
from .config import RuntimeSettings
from .executor import ActionExecutor
from .ops_client import OpsClient
from .sessions import SessionState, get_session_registry
from .telephony import get_telephony_adapter

logger = logging.getLogger(__name__)

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
SOURCE = "voice-web"


class StartCallRequest(BaseModel):
    caller_number: str | None = None


class TurnBody(BaseModel):
    text: str = Field(min_length=1)


def _err(status: int, code: str, message: str, retryable: bool = False, details: dict | None = None) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, retryable=retryable, details=details or {}))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class VoiceWebApi:
    """State and behaviour of the browser voice API (one instance per runtime)."""

    def __init__(self, settings: RuntimeSettings, ops_client: OpsClient) -> None:
        self.s = settings
        self.ops = ops_client
        self._idle_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle (called from app.py's lifespan)
    async def start(self) -> None:
        if self._idle_task is None:
            self._idle_task = asyncio.create_task(self._idle_loop())

    async def stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        for session in self._voice_sessions():                  # shutting down: close every open call in ops
            await self.finalize(session, reason="shutdown")

    # ------------------------------------------------------------------ helpers
    def _voice_sessions(self) -> list[SessionState]:
        return [s for s in get_session_registry().all() if s.source == SOURCE and not s.finalized]

    def _authorised(self, request: Request) -> bool:
        key = self.s.voice_web_api_key
        if not key:
            return True                                           # local development: no key configured
        given = request.headers.get("authorization", "")
        return given.startswith("Bearer ") and hmac.compare_digest(given[7:].strip().encode(), key.encode())

    async def finalize(self, session: SessionState, reason: str) -> dict[str, Any]:
        """Close a call exactly once: wait for background SMS, end the Brain session, post /end to ops, forget the session."""
        if session.finalized:
            return {"status": "already_ended", "call_id": session.call_id}
        session.finalized = True
        session.is_ended = True
        task = session.active_stream_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            if session.active_turn_interruptable:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            else:                                                 # emergency / ticket turn: let it finish (CR-8), but not forever
                await asyncio.wait({task}, timeout=10.0)
        await session.drain_background_tasks()
        outcome = CallOutcome.abandoned if session.turn_count == 0 else session.derive_outcome()
        ai_resolved = outcome == CallOutcome.answered
        try:
            await get_brain(self.s).end_call(session.call_id)
        except Exception as exc:                                  # never let a Brain error keep the ops call open
            logger.warning("brain.end_call failed for %s: %s", session.call_id, type(exc).__name__)
        await self.ops.post_call_end(session.call_id, CallEnd(ended_at=datetime.now(timezone.utc), outcome=outcome,
                                                              ai_resolved=ai_resolved, turns_count=session.turn_count))
        get_session_registry().remove(session.call_id)
        logger.info("voice call %s ended (%s, outcome=%s)", session.call_id, reason, outcome.value)
        return {"status": "ended", "call_id": session.call_id, "outcome": outcome.value, "ai_resolved": ai_resolved,
                "turns_count": session.turn_count}

    async def sweep_idle(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        n = 0
        for session in self._voice_sessions():
            busy = session.active_stream_task is not None and not session.active_stream_task.done()
            if not busy and (now - session.last_activity).total_seconds() > self.s.voice_web_idle_timeout_s:
                await self.finalize(session, reason="idle_timeout")
                n += 1
        return n

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(max(2.0, min(15.0, self.s.voice_web_idle_timeout_s / 4)))
            try:
                await self.sweep_idle()
            except Exception as exc:
                logger.warning("idle sweep failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------ one turn as a stream of (event, data)
    async def turn_events(self, session: SessionState, text: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        brain = get_brain(self.s)
        turn_index = session.next_turn_index()
        req = TurnRequest(call_id=session.call_id, turn_index=turn_index, user_text=text, history=[], dtmf=None,
                          received_at=datetime.now(timezone.utc))
        executor = ActionExecutor(self.ops, get_telephony_adapter(self.s), self.s, session)
        lang, n_speech, tickets, t0 = "hinglish", 0, [], time.perf_counter()
        agen = brain.handle_turn(req)
        try:
            async for ev in agen:
                if ev.type == "meta":
                    session.active_turn_interruptable = ev.interruptable
                    lang = ev.lang.value if hasattr(ev.lang, "value") else str(ev.lang)
                    yield "meta", {"turn_index": turn_index, "path": getattr(ev.path, "value", str(ev.path)), "tier": getattr(ev.tier, "value", str(ev.tier)),
                                   "lang": lang, "interruptable": ev.interruptable}
                elif ev.type == "action":
                    await executor.execute(ev.action, lang=lang, blocking=getattr(ev, "blocking", True))
                    info = self._action_info(ev.action, executor)
                    if info["kind"] == "create_ticket" and executor.ticket_id and executor.ticket_id not in tickets:
                        tickets.append(executor.ticket_id)
                    yield "action", info
                    while executor.injected_speech:
                        yield "speech", {"index": n_speech, "text": executor.injected_speech.pop(0)}
                        n_speech += 1
                elif ev.type == "speech":
                    spoken = executor.process_speech_text(ev.text, lang=lang)
                    if spoken:
                        yield "speech", {"index": n_speech, "text": spoken}
                        n_speech += 1
                elif ev.type == "done":
                    summary = ev.summary
                    session.add_background_task(asyncio.create_task(self.ops.post_turn(session.call_id, summary)))
                    yield "done", {"turn_index": turn_index, "path": getattr(summary.path, "value", str(summary.path)),
                                   "tier": getattr(summary.tier, "value", str(summary.tier)), "lang": lang,
                                   "rule_id": summary.rule_id, "priority": getattr(summary.priority, "value", summary.priority),
                                   "ticket_ids": tickets, "ended": session.is_ended, "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("voice turn failed for %s: %s", session.call_id, type(exc).__name__, exc_info=True)
            yield "error", {"code": "internal", "message": "The assistant could not complete this turn.", "retryable": True}
        finally:
            session.last_activity = datetime.now(timezone.utc)
            if hasattr(agen, "aclose"):
                try:
                    await agen.aclose()
                except Exception:
                    pass

    @staticmethod
    def _action_info(action: Any, executor: ActionExecutor) -> dict[str, Any]:
        """What the page may know about an action: no phone numbers, no SMS bodies, no free text."""
        kind = action.kind
        info: dict[str, Any] = {"kind": kind}
        if kind == "create_ticket":
            t = action.ticket
            info.update(ticket_id=executor.ticket_id, type=getattr(t.type, "value", str(t.type)),
                        priority=getattr(t.priority, "value", str(t.priority)), department=t.department)
        elif kind == "transfer":
            info.update(department=action.department, simulated=True)
        elif kind == "send_sms":
            info.update(template_id=action.template_id)
        return info

    # ------------------------------------------------------------------ HTTP app
    def build_app(self) -> FastAPI:
        api = FastAPI(title="Resolvix voice-web API", version="1", docs_url=None, redoc_url=None, openapi_url=None)
        voice = self
        registry = get_session_registry()

        @api.exception_handler(RequestValidationError)
        async def _validation(_request: Request, exc: RequestValidationError):
            return _err(422, "validation_error", "The request body is not valid.", details={"fields": ",".join(".".join(str(p) for p in e["loc"][1:]) for e in exc.errors())})

        @api.middleware("http")
        async def _auth(request: Request, call_next):
            if request.method != "OPTIONS" and not voice._authorised(request):      # CORS preflight carries no credentials
                return _err(401, "unauthorized", "Missing or invalid bearer token.")
            return await call_next(request)

        def _session(call_id: str) -> SessionState | None:
            s = registry.get(call_id)
            return s if s is not None and s.source == SOURCE and not s.finalized else None

        @api.get("/config")
        async def config():
            return {"api_version": 1, "contract_version": CONTRACT_VERSION, "streaming": True, "idle_timeout_s": voice.s.voice_web_idle_timeout_s,
                    "max_text_chars": voice.s.voice_web_max_text_chars, "caller_number_allowed": voice.s.voice_web_allow_caller_number}

        @api.post("/calls", status_code=201)
        async def start_call(body: StartCallRequest | None = None):
            body = body or StartCallRequest()
            if body.caller_number:
                if not voice.s.voice_web_allow_caller_number:
                    return _err(422, "validation_error", "caller_number is not accepted by this server.")
                if not E164.fullmatch(body.caller_number):
                    return _err(422, "validation_error", "caller_number must be E.164, for example +919876543210.")
            if len(voice._voice_sessions()) >= voice.s.voice_web_max_calls:
                return _err(429, "rate_limited", "Too many calls are active. Try again shortly.", retryable=True)
            call_id = f"call-voice-{uuid.uuid4().hex[:10]}"
            session, _ = registry.get_or_create(call_id=call_id, channel=CallChannel.web, caller_number=body.caller_number)
            session.source = SOURCE
            try:
                plan = await get_brain(voice.s).start_call(CallContext(call_id=call_id, channel=CallChannel.web, caller_number=body.caller_number,
                                                                       started_at=session.started_at))
            except Exception as exc:
                registry.remove(call_id)
                logger.error("start_call failed: %s", type(exc).__name__)
                return _err(500, "internal", "The assistant is not available.", retryable=True)
            await voice.ops.post_call_start(CallStart(call_id=call_id, channel=CallChannel.web, caller_number=body.caller_number,
                                                      started_at=session.started_at, kb_version=plan.kb_version, lang=plan.greeting_lang))
            lang = getattr(plan.greeting_lang, "value", str(plan.greeting_lang))
            return {"call_id": call_id, "greeting_text": plan.greeting_text, "lang": lang, "kb_version": plan.kb_version,
                    "interruptable": plan.interruptable, "idle_timeout_s": voice.s.voice_web_idle_timeout_s,
                    "max_text_chars": voice.s.voice_web_max_text_chars}

        @api.post("/calls/{call_id}/turns")
        async def turn(call_id: str, body: TurnBody, request: Request, stream: bool | None = None):
            session = _session(call_id)
            if session is None:
                return _err(404, "not_found", "Unknown or ended call.")
            text = body.text.strip()
            if not text:
                return _err(422, "validation_error", "text is empty.")
            if len(text) > voice.s.voice_web_max_text_chars:
                return _err(422, "validation_error", f"text is longer than {voice.s.voice_web_max_text_chars} characters.")
            prev = session.active_stream_task
            if prev is not None and not prev.done():
                if not session.active_turn_interruptable:
                    return _err(409, "conflict", "The previous turn cannot be interrupted yet.", retryable=True)
                prev.cancel()                                                    # a new turn is a barge-in on the one being spoken
                await asyncio.gather(prev, return_exceptions=True)
            want_stream = stream if stream is not None else "text/event-stream" in request.headers.get("accept", "") or "application/json" not in request.headers.get("accept", "")
            queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

            async def pump() -> None:
                try:
                    async for item in voice.turn_events(session, text):
                        await queue.put(item)
                except asyncio.CancelledError:
                    await queue.put(("error", {"code": "conflict", "message": "The turn was cancelled (barge-in).", "retryable": False}))
                    raise
                finally:
                    await queue.put(None)
                    if session.is_ended and not session.finalized:
                        asyncio.create_task(voice.finalize(session, reason="end_call_action"))

            task = asyncio.create_task(pump())
            session.active_stream_task = task

            if want_stream:
                async def events():
                    try:
                        while True:
                            item = await queue.get()
                            if item is None:
                                break
                            yield _sse(*item)
                    finally:                                                       # the browser went away
                        if not task.done() and session.active_turn_interruptable:
                            task.cancel()                                          # barge-in / tab closed; a non-interruptable turn runs to its end (CR-8)
                return StreamingResponse(events(), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

            sentences: list[str] = []
            actions: list[dict] = []
            meta: dict = {}
            done: dict = {}
            error: dict | None = None
            while True:
                item = await queue.get()
                if item is None:
                    break
                name, data = item
                if name == "speech":
                    sentences.append(data["text"])
                elif name == "action":
                    actions.append(data)
                elif name == "meta":
                    meta = data
                elif name == "done":
                    done = data
                elif name == "error":
                    error = data
            if error is not None and not done:
                return _err(500 if error["code"] == "internal" else 409, error["code"], error["message"], retryable=bool(error.get("retryable")))
            return {"call_id": call_id, "turn_index": done.get("turn_index", meta.get("turn_index")), "reply_text": " ".join(sentences).strip(),
                    "sentences": sentences, "lang": done.get("lang", meta.get("lang")), "path": done.get("path", meta.get("path")),
                    "tier": done.get("tier", meta.get("tier")), "rule_id": done.get("rule_id"), "priority": done.get("priority"),
                    "interruptable": meta.get("interruptable", True), "actions": actions, "ticket_ids": done.get("ticket_ids", []),
                    "ended": done.get("ended", False)}

        @api.post("/calls/{call_id}/cancel")
        async def cancel(call_id: str):
            session = _session(call_id)
            if session is None:
                return _err(404, "not_found", "Unknown or ended call.")
            task = session.active_stream_task
            if task is None or task.done():
                return {"call_id": call_id, "cancelled": False, "reason": "no_turn_in_progress"}
            if not session.active_turn_interruptable:
                return {"call_id": call_id, "cancelled": False, "reason": "not_interruptable"}
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            session.last_activity = datetime.now(timezone.utc)
            return {"call_id": call_id, "cancelled": True}

        @api.post("/calls/{call_id}/end")
        async def end(call_id: str):
            session = registry.get(call_id)
            if session is None or session.source != SOURCE:
                return _err(404, "not_found", "Unknown call.")
            return await voice.finalize(session, reason="client_end")

        # added last so it is the outermost layer: even 401/422 answers carry CORS headers and the browser can read them
        origins = [o.strip() for o in self.s.voice_web_cors_origins.split(",") if o.strip()]
        api.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Authorization", "Content-Type", "Accept"], expose_headers=[], max_age=600)
        return api
