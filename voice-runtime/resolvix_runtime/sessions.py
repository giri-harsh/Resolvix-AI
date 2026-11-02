from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from resolvix_contracts.models import CallChannel, CallOutcome

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    call_id: str
    channel: CallChannel = CallChannel.web
    caller_number: str | None = None
    turn_count: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_stream_task: asyncio.Task | None = None
    active_turn_interruptable: bool = True
    created_tickets: list[str] = field(default_factory=list)
    transfers_connected: bool = False
    emergency: bool = False
    is_ended: bool = False
    agora_agent_id: str | None = None
    agora_channel: str | None = None
    background_tasks: set[asyncio.Task] = field(default_factory=set)
    source: str = "runtime"      # "voice-web" for calls opened through /api/voice
    finalized: bool = False      # the call has been ended and posted to ops (exactly once)

    def add_background_task(self, task: asyncio.Task) -> None:
        """Register a background task with a strong reference until completion."""
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def drain_background_tasks(self) -> None:
        """Safely await any remaining background tasks before session cleanup."""
        if self.background_tasks:
            await asyncio.gather(*list(self.background_tasks), return_exceptions=True)
            self.background_tasks.clear()

    def next_turn_index(self) -> int:
        self.turn_count += 1
        self.last_activity = datetime.now(timezone.utc)
        self.active_turn_interruptable = True
        return self.turn_count

    def derive_outcome(self) -> CallOutcome:
        if self.transfers_connected:
            return CallOutcome.transferred
        if self.emergency:
            return CallOutcome.emergency
        if self.created_tickets:
            return CallOutcome.escalated
        return CallOutcome.answered


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()

    def get_or_create(
        self,
        call_id: str,
        channel: CallChannel = CallChannel.web,
        caller_number: str | None = None,
    ) -> tuple[SessionState, bool]:
        """Returns (session, is_new)."""
        is_new = False
        if call_id not in self._sessions:
            self._sessions[call_id] = SessionState(
                call_id=call_id,
                channel=channel,
                caller_number=caller_number,
            )
            is_new = True
        return self._sessions[call_id], is_new

    def get(self, call_id: str) -> SessionState | None:
        return self._sessions.get(call_id)

    def all(self) -> list[SessionState]:
        return list(self._sessions.values())

    def register_task(self, call_id: str, task: asyncio.Task) -> None:
        """Register the active SSE streaming task. Cancels any currently active task for barge-in if interruptable."""
        session = self._sessions.get(call_id)
        if session is None:
            return

        if not session.active_turn_interruptable:
            logger.info("Call %s is in non-interruptable turn. Skipping barge-in cancellation.", call_id)
            return

        if session.active_stream_task is not None and not session.active_stream_task.done():
            logger.info("Barge-in detected for call_id=%s. Cancelling active stream task.", call_id)
            session.active_stream_task.cancel()

        session.active_stream_task = task


    def remove(self, call_id: str) -> SessionState | None:
        return self._sessions.pop(call_id, None)


_REGISTRY: SessionRegistry | None = None


def get_session_registry() -> SessionRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SessionRegistry()
    return _REGISTRY
