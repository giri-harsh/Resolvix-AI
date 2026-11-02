from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from resolvix_contracts import TICKET_ID_PLACEHOLDER, speak_ticket_id
from resolvix_contracts.models import (
    Action,
    CallChannel,
    CreateTicketAction,
    EndCallAction,
    GapCreate,
    LogGapAction,
    ScheduleCallbackAction,
    SendSmsAction,
    SmsRequest,
    TicketCreate,
    TicketEvent,
    TicketType,
    TransferAction,
)
from .config import RuntimeSettings
from .ops_client import OpsClient
from .sessions import SessionState
from .telephony.base import TelephonyAdapter

logger = logging.getLogger(__name__)


class ActionExecutor:
    """Carries out brain action events safely, in order, maintaining turn state."""

    def __init__(
        self,
        ops_client: OpsClient,
        telephony: TelephonyAdapter,
        settings: RuntimeSettings,
        session: SessionState,
    ) -> None:
        self.ops_client = ops_client
        self.telephony = telephony
        self.settings = settings
        self.session = session
        self.ticket_id: str | None = None
        self.ops_unreachable: bool = False
        self.injected_speech: list[str] = []
        self.last_sms_status: str | None = None
        self.pending_tasks: list[asyncio.Task] = []

    async def execute(self, action: Action, lang: str = "hinglish", blocking: bool = True) -> None:
        kind = action.kind
        if kind == "create_ticket":
            assert isinstance(action, CreateTicketAction)
            await asyncio.shield(self._handle_create_ticket(action.ticket))

        elif kind == "transfer":
            assert isinstance(action, TransferAction)
            await self._handle_transfer(action, lang)
        elif kind == "send_sms":
            assert isinstance(action, SendSmsAction)
            # In emergency path or when non-blocking, send SMS concurrently in background
            # so the caller's emergency transfer or next actions are never delayed awaiting SMS delivery.
            if not blocking or self.session.emergency:
                task = asyncio.create_task(self._handle_send_sms(action))
                self.session.add_background_task(task)
                self.pending_tasks.append(task)
            else:
                await self._handle_send_sms(action)
        elif kind == "log_gap":
            assert isinstance(action, LogGapAction)
            await self._handle_log_gap(action)
        elif kind == "schedule_callback":
            assert isinstance(action, ScheduleCallbackAction)
            await self._handle_schedule_callback(action)
        elif kind == "end_call":
            assert isinstance(action, EndCallAction)
            await self._handle_end_call(action)

    async def _handle_create_ticket(self, ticket: TicketCreate) -> None:
        if ticket.type == TicketType.EMERGENCY:
            self.session.emergency = True

        ticket.call_id = self.session.call_id
        if self.session.caller_number and not ticket.caller_number:
            ticket.caller_number = self.session.caller_number

        created = await self.ops_client.create_ticket(ticket)
        if created is not None:
            self.ticket_id = created.ticket_id
            self.session.created_tickets.append(self.ticket_id)
            logger.info("Created ticket successfully: %s", self.ticket_id)
        else:
            # ops-api unreachable: assign TMP-<4 random digits> and speak registered_no_id
            tmp_id = f"TMP-{random.randint(1000, 9999)}"
            self.ticket_id = tmp_id
            self.ops_unreachable = True
            self.session.created_tickets.append(tmp_id)
            logger.warning("ops-api unreachable. Assigned temporary ID: %s", tmp_id)

    async def _handle_transfer(self, action: TransferAction, lang: str) -> None:
        # Ticket-first rule check
        if action.requires_ticket and not self.ticket_id:
            logger.error(
                "Ticket-first violation: transfer requested before ticket created! Treating as transfer_failed."
            )
            return

        now = datetime.now(timezone.utc)
        target_ticket_id = self.ticket_id or "NO_TICKET"

        # Channel in (web, test): simulate
        if self.session.channel in (CallChannel.web, CallChannel.test):
            logger.info(
                "Channel %s is non-PSTN. Simulating transfer to %s for ticket %s.",
                self.session.channel,
                action.to_number,
                target_ticket_id,
            )
            await self.ops_client.post_ticket_event(
                target_ticket_id,
                TicketEvent(
                    kind="transfer_attempted",
                    payload={"mode": "simulated", "to_number": action.to_number},
                    actor="system",
                    at=now,
                ),
            )
            await self.ops_client.post_ticket_event(
                target_ticket_id,
                TicketEvent(
                    kind="transfer_failed",
                    payload={"reason": "simulated", "to_number": action.to_number},
                    actor="system",
                    at=now,
                ),
            )
            if self.session.channel == CallChannel.test:
                sim_line = self.settings.get_runtime_line("transfer_simulated", lang)
                if sim_line:
                    self.injected_speech.append(sim_line)
            return

        # PSTN channel: attempt real transfer
        await self.ops_client.post_ticket_event(
            target_ticket_id,
            TicketEvent(
                kind="transfer_attempted",
                payload={"to_number": action.to_number, "reason": action.reason},
                actor="system",
                at=now,
            ),
        )

        success = await self.telephony.transfer(self.session.call_id, action.to_number)
        if success:
            self.session.transfers_connected = True
            await self.ops_client.post_ticket_event(
                target_ticket_id,
                TicketEvent(
                    kind="transfer_connected",
                    payload={"to_number": action.to_number},
                    actor="system",
                    at=datetime.now(timezone.utc),
                ),
            )
        else:
            await self.ops_client.post_ticket_event(
                target_ticket_id,
                TicketEvent(
                    kind="transfer_failed",
                    payload={"to_number": action.to_number, "reason": "unavailable"},
                    actor="system",
                    at=datetime.now(timezone.utc),
                ),
            )
            # Inject extra speech chunk from runtime_lines.yaml -> transfer_failed
            failed_template = self.settings.get_runtime_line("transfer_failed", lang)
            spoken_id = speak_ticket_id(target_ticket_id, lang)
            failed_line = failed_template.replace("{ticket_id_spoken}", spoken_id)
            self.injected_speech.append(failed_line)

    async def _handle_send_sms(self, action: SendSmsAction) -> None:
        sms_vars = dict(action.vars)
        if self.ticket_id:
            for k, v in list(sms_vars.items()):
                if TICKET_ID_PLACEHOLDER in str(v):
                    sms_vars[k] = str(v).replace(TICKET_ID_PLACEHOLDER, self.ticket_id)
            if "ticket_id" in sms_vars:
                sms_vars["ticket_id"] = self.ticket_id

        sms_req = SmsRequest(
            idempotency_key=action.idempotency_key,
            to_number=action.to_number,
            template_id=action.template_id,
            vars=sms_vars,
            consent=action.consent,
            call_id=self.session.call_id,
            ticket_id=self.ticket_id,
        )
        try:
            res = await self.ops_client.post_sms(sms_req)
            if res is not None:
                self.last_sms_status = res.status
                if res.status in ("queued", "sent", "delivered"):
                    logger.info(
                        "SMS %s for %s (id: %s, mode: %s)",
                        res.status,
                        action.to_number,
                        res.message_id,
                        res.mode,
                    )
                elif res.status == "suppressed":
                    logger.info(
                        "SMS suppressed for %s: %s (id: %s)",
                        action.to_number,
                        res.reason,
                        res.message_id,
                    )
                else:
                    logger.warning(
                        "SMS failed for %s: %s (id: %s)",
                        action.to_number,
                        res.reason,
                        res.message_id,
                    )
            else:
                self.last_sms_status = "failed"
                logger.warning("ops.post_sms returned None (queued in retry queue)")
        except Exception as e:
            self.last_sms_status = "failed"
            logger.warning("Background SMS task error for %s: %s", action.to_number, e)

    async def _handle_log_gap(self, action: LogGapAction) -> None:
        gap = GapCreate(
            question=action.question,
            lang=action.lang,
            department_guess=action.department_guess,
            call_id=self.session.call_id,
            ticket_id=self.ticket_id,
        )
        await self.ops_client.post_gap(gap)

    async def _handle_schedule_callback(self, action: ScheduleCallbackAction) -> None:
        if not self.ticket_id:
            return
        await self.ops_client.post_ticket_event(
            self.ticket_id,
            TicketEvent(
                kind="callback_scheduled",
                payload={"to_number": action.to_number, "reason": action.reason},
                actor="system",
                at=datetime.now(timezone.utc),
            ),
        )

    async def _handle_end_call(self, action: EndCallAction) -> None:
        self.session.is_ended = True

    def process_speech_text(self, text: str, lang: str = "hinglish") -> str:
        """Substitute ticket_id placeholder or replace with registered_no_id if ops is down."""
        if self.ops_unreachable:
            return self.settings.get_runtime_line("registered_no_id", lang)

        if self.ticket_id:
            spoken_id = speak_ticket_id(self.ticket_id, lang)
            text = text.replace(TICKET_ID_PLACEHOLDER, spoken_id)
            text = text.replace("{{ticket_id}}", spoken_id)

        return text

    async def wait_pending(self) -> None:
        """Await all background tasks (such as asynchronous SMS) to complete cleanly."""
        if self.pending_tasks:
            await asyncio.gather(*self.pending_tasks, return_exceptions=True)
            self.pending_tasks.clear()
