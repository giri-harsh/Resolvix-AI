"""The phone bridge: one Exotel Voicebot WebSocket = one call = one isolated session (own Brain call, own ASR/TTS, own tasks).

Protocol facts are isolated in ExotelProtocol. Field names follow Exotel's AgentStream docs but the docs disagree in places (media_format
encoding/bit_rate) and nothing here has been checked against a live call: treat ExotelProtocol as the only place to adjust.
Closing this WebSocket is NOT assumed to end the phone call (no documented hang-up); the service decides terminal states.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from resolvix_contracts.models import CallChannel, CallContext, CallStart

from ..brain_loader import get_brain
from ..sessions import get_session_registry
from .audio import b64decode_pcm, b64encode_pcm, chunk_for_exotel, is_voiced
from .models import CallStatus
from .speech import AsrAdapter, SpeechError, TtsAdapter

log = logging.getLogger("resolvix_runtime.outbound.bridge")
MAX_MESSAGE_CHARS = 256 * 1024
FALLBACK = {"en": "Sorry, we are having a technical problem. We will call you back.",
            "hi": "Maaf kijiye, abhi thodi dikkat hai. Hum aapko call back karenge.",
            "hinglish": "Maaf kijiye, abhi thodi dikkat hai. Hum aapko call back karenge."}


class ExotelProtocol:
    """Parse and build Voicebot messages. UNVERIFIED against a live call: see the module docstring."""

    @staticmethod
    def parse(raw: str) -> dict[str, Any]:
        msg = json.loads(raw)
        if not isinstance(msg, dict) or not isinstance(msg.get("event"), str):
            raise ValueError("not a Voicebot event")
        return msg

    @staticmethod
    def start_info(msg: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        start = msg.get("start") if isinstance(msg.get("start"), dict) else {}
        return (str(start.get("stream_sid") or msg.get("stream_sid") or ""), str(start.get("call_sid") or ""),
                start.get("custom_parameters") if isinstance(start.get("custom_parameters"), dict) else {})

    @staticmethod
    def media_out(stream_sid: str, pcm: bytes) -> dict[str, Any]:
        return {"event": "media", "stream_sid": stream_sid, "media": {"payload": b64encode_pcm(pcm)}}

    @staticmethod
    def mark_out(stream_sid: str, name: str) -> dict[str, Any]:
        return {"event": "mark", "stream_sid": stream_sid, "mark": {"name": name}}

    @staticmethod
    def clear_out(stream_sid: str) -> dict[str, Any]:
        return {"event": "clear", "stream_sid": stream_sid}


class BridgeSession:
    def __init__(self, service, row: dict[str, Any], ws: WebSocket, asr: AsrAdapter, tts: TtsAdapter) -> None:
        self.service, self.row, self.ws, self.asr, self.tts = service, row, ws, asr, tts
        self.call_id: str = row["call_id"]
        self.lang: str = row["lang"]
        self.stream_sid = ""
        self.started = False
        self.closed = False
        self._outstanding = 0                     # marks we asked for that Exotel has not yet confirmed as played
        self._mark_seq = 0
        self._interruptable = True
        self._turn_task: asyncio.Task | None = None
        self._pending: list[str] = []
        self.session = None                       # the Brain-side SessionState (isolated per call)
        self.marks_seen = 0
        self.ended_by_stop = False

    @property
    def playing(self) -> bool:
        """Audio is still queued or playing on the phone side (a `mark` has not come back for it yet)."""
        return self._outstanding > 0

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        await self.ws.accept()
        self.service.bridges[self.call_id] = self
        try:
            while not self.closed:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                text = msg.get("text")
                if text is None:
                    continue
                if len(text) > MAX_MESSAGE_CHARS:
                    await self._reject(1009, "message_too_large")
                    break
                try:
                    event = ExotelProtocol.parse(text)
                except (ValueError, json.JSONDecodeError):
                    await self._reject(1003, "malformed_message")
                    break
                await self._dispatch(event)
        except WebSocketDisconnect:
            pass
        finally:
            await self._teardown()

    async def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        if kind == "start":
            if not self.started:
                await self._on_start(event)
        elif kind == "media":
            if not self.started:
                await self._reject(1002, "media_before_start")
            else:
                await self._on_media(event)
        elif kind == "mark":
            self.marks_seen += 1
            self._outstanding = max(0, self._outstanding - 1)
            if self._outstanding == 0 and not (self._turn_task and not self._turn_task.done()):
                self._interruptable = True            # the greeting / last answer has been heard; the caller now has the floor
        elif kind == "stop":
            await self._on_stop(event)
        # connected / dtmf / unknown events are accepted and ignored

    # ------------------------------------------------------------------ events
    async def _on_start(self, event: dict[str, Any]) -> None:
        svc = self.service
        stream_sid, call_sid, custom = ExotelProtocol.start_info(event)
        foreign = [v for v in custom.values() if isinstance(v, str) and v.startswith("call-out-") and v != self.call_id]
        if not stream_sid or call_sid != self.row["provider_sid"] or foreign:
            svc.audit(self.call_id, "ws_rejected", None, reason="start_mismatch")
            await self._reject(1008, "start_mismatch")
            return
        if not svc.store.set_status(self.call_id, CallStatus.connected, svc.clock(), stream_sid=stream_sid, ws_started=1):
            svc.audit(self.call_id, "ws_rejected", None, reason="illegal_state")
            await self._reject(1008, "illegal_state")
            return
        self.started, self.stream_sid = True, stream_sid
        svc.audit(self.call_id, "connected", None, stream_sid=stream_sid)
        try:
            number = svc.number_for(self.call_id)
            registry = get_session_registry()
            self.session, _ = registry.get_or_create(call_id=self.call_id, channel=CallChannel.pstn, caller_number=number)
            self.session.source = "outbound"
            plan = await get_brain(svc.rs).start_call(CallContext(call_id=self.call_id, channel=CallChannel.pstn, caller_number=number,
                                                                  started_at=self.session.started_at))
            await svc.ops.post_call_start(CallStart(call_id=self.call_id, channel=CallChannel.pstn, caller_number=number,
                                                    started_at=self.session.started_at, kb_version=plan.kb_version, lang=plan.greeting_lang))
        except Exception as exc:
            log.error("outbound %s brain start failed: %s", self.call_id, type(exc).__name__)
            await svc.finish(self.call_id, CallStatus.failed, "brain_unavailable")
            return
        self._interruptable = bool(plan.interruptable)
        self._turn_task = asyncio.create_task(self._guard(self._speak([plan.greeting_text])))

    async def _on_media(self, event: dict[str, Any]) -> None:
        try:
            pcm = b64decode_pcm(str((event.get("media") or {}).get("payload", "")))
        except ValueError:
            await self._reject(1007, "bad_media")
            return
        if self.playing and self._interruptable and is_voiced(pcm):
            await self._barge_in()
        try:
            finals = await self.asr.feed(pcm)
        except SpeechError:
            await self._speak_fallback()
            await self.service.finish(self.call_id, CallStatus.failed, "asr_failure")
            return
        for text in finals:
            await self._on_transcript(text)

    async def _on_stop(self, event: dict[str, Any]) -> None:
        reason = str((event.get("stop") or {}).get("reason", ""))
        self.ended_by_stop = True
        task = self._turn_task
        if task is not None and not task.done():
            # Let the turn in flight record itself; an emergency/ticket turn (not interruptable, CR-8) is given long enough to finish its actions.
            await asyncio.wait({task}, timeout=2.0 if self._interruptable else 10.0)
        self.service.audit(self.call_id, "ws_stop", None, reason=reason[:20])
        # "callended" = the caller hung up: the call is over. "stopped" = the applet ended: we do not know the phone leg is closed.
        await self.service.finish(self.call_id, CallStatus.ended, f"ws_stop:{reason}"[:60], hangup_confirmed=1 if reason == "callended" else 0)
        self.closed = True

    # ------------------------------------------------------------------ conversation
    async def _on_transcript(self, text: str) -> None:
        prev = self._turn_task
        if prev is not None and not prev.done():
            if not self._interruptable:
                self._pending.append(text)          # emergency / ticket turns finish first (CR-8); the new speech waits
                return
            prev.cancel()
            await asyncio.gather(prev, return_exceptions=True)
        self._turn_task = asyncio.create_task(self._guard(self._run_turn(text)))

    async def _run_turn(self, text: str) -> None:
        while True:
            async for event, data in self.service.voice.turn_events(self.session, text):
                if event == "meta":
                    self._interruptable = bool(data.get("interruptable", True))
                elif event == "speech":
                    await self._speak([data["text"]])
                elif event == "error":
                    await self._speak_fallback()
                elif event == "done" and data.get("ended"):
                    await self.service.finish(self.call_id, CallStatus.ended, "brain_end_call")
                    return
            if self._outstanding == 0:
                self._interruptable = True            # otherwise the `mark` handler re-opens it once the audio has really been played
            if not self._pending:
                return
            text = self._pending.pop(0)

    async def _guard(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except SpeechError:
            await self.service.finish(self.call_id, CallStatus.failed, "tts_failure")
        except Exception as exc:
            log.error("outbound %s turn failed: %s", self.call_id, type(exc).__name__)

    async def _speak(self, sentences: list[str]) -> None:
        for sentence in sentences:
            pcm = await self.tts.synthesize(sentence, self.lang)
            for chunk in chunk_for_exotel(pcm, self.service.cfg.chunk_size):
                await self._send(ExotelProtocol.media_out(self.stream_sid, chunk))
                await asyncio.sleep(0)
            self._mark_seq += 1
            self._outstanding += 1
            await self._send(ExotelProtocol.mark_out(self.stream_sid, f"m{self._mark_seq}"))

    async def _speak_fallback(self) -> None:
        try:
            await self._speak([FALLBACK.get(self.lang, FALLBACK["en"])])
        except SpeechError:
            pass

    async def _barge_in(self) -> None:
        """The caller spoke over the bot: drop queued audio and cancel the turn being spoken (not allowed for non-interruptable turns)."""
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._outstanding = 0
        await self._send(ExotelProtocol.clear_out(self.stream_sid))
        self.service.audit(self.call_id, "barge_in", None)

    # ------------------------------------------------------------------ helpers
    async def _send(self, obj: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            await self.ws.send_text(json.dumps(obj, separators=(",", ":")))
        except Exception:
            self.closed = True

    async def _reject(self, code: int, reason: str) -> None:
        self.closed = True
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass

    async def close(self, reason: str) -> None:
        """Close OUR side (idempotent). This does not claim the phone call is over."""
        if self.closed:
            return
        self.closed = True
        task = self._turn_task
        if task is not None and not task.done() and task is not asyncio.current_task() and self._interruptable:
            task.cancel()                                                   # never the task that is closing us; never an emergency/ticket turn (CR-8)
        try:
            await self.ws.close(code=1000, reason=reason[:60])
        except Exception:
            pass

    async def _teardown(self) -> None:
        self.closed = True
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await self.asr.aclose()
        except Exception:
            pass
        if self.service.bridges.get(self.call_id) is self:
            self.service.bridges.pop(self.call_id, None)
        if self.started and not self.ended_by_stop:
            self.service.audit(self.call_id, "ws_disconnected", None)      # the carrier callback or the watchdog decides what happens next
