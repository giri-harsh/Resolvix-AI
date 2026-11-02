from __future__ import annotations

import asyncio
import logging
from typing import Any
import httpx
from pydantic import TypeAdapter

from resolvix_contracts.models import (
    CallEnd,
    CallStart,
    ErrorEnvelope,
    GapCreate,
    SmsRequest,
    SmsResult,
    Ticket,
    TicketCreate,
    TicketCreated,
    TicketEvent,
    TicketRef,
    TurnSummary,
)
from .config import RuntimeSettings

logger = logging.getLogger(__name__)

ERROR_ENVELOPE_ADAPTER = TypeAdapter(ErrorEnvelope)
TICKET_CREATED_ADAPTER = TypeAdapter(TicketCreated)
SMS_RESULT_ADAPTER = TypeAdapter(SmsResult)


class OpsClient:
    """Async client for Resolvix ops-api (Member C) with retry queue."""

    def __init__(self, settings: RuntimeSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.base_url = settings.ops_api_url.rstrip("/")
        self.service_key = settings.service_key
        self._external_client = client
        self.retry_queue: list[dict[str, Any]] = []
        self._flush_task: asyncio.Task | None = None
        self._closed = False
        self._shared: httpx.AsyncClient | None = None          # one pooled client for every ops call (see _get_client)
        self._shared_loop: asyncio.AbstractEventLoop | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "X-Service-Key": self.service_key,
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None and not self._external_client.is_closed:
            return self._external_client
        # One pooled client for the life of the runtime. It used to be a NEW AsyncClient per request, which builds an SSL context every
        # time (about 0.35 to 0.4 s on the demo machine, even for http://), never closed, and sat in front of every ops write
        # (create_ticket blocks the emergency transfer). A client is bound to the event loop it was first used on, so a different
        # loop (tests, reloads) gets a fresh one.
        loop = asyncio.get_running_loop()
        if self._shared is None or self._shared.is_closed or self._shared_loop is not loop:
            self._shared = httpx.AsyncClient(timeout=2.0, limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
            self._shared_loop = loop
        return self._shared

    async def start(self) -> None:
        """Start the background retry-queue flusher."""
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._periodic_flush())

    async def close(self) -> None:
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        if self._external_client is not None and not self._external_client.is_closed:
            await self._external_client.aclose()
        if self._shared is not None and not self._shared.is_closed and self._shared_loop is asyncio.get_running_loop():
            await self._shared.aclose()

    async def _periodic_flush(self) -> None:
        while not self._closed:
            try:
                await asyncio.sleep(5.0)
                await self.flush_retry_queue()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error during retry queue flush: %s", e)

    async def flush_retry_queue(self) -> int:
        """Attempt to flush queued non-blocking requests. Returns number of drained items."""
        if not self.retry_queue:
            return 0

        pending = list(self.retry_queue)
        self.retry_queue.clear()
        flushed_count = 0

        for item in pending:
            item_type = item.get("type")
            payload = item.get("payload")
            success = False
            try:
                if item_type == "turn":
                    success = await self.post_turn(item["call_id"], payload, queue_on_failure=False)
                elif item_type == "gap":
                    success = await self.post_gap(payload, queue_on_failure=False)
                elif item_type == "sms":
                    res = await self.post_sms(payload, queue_on_failure=False)
                    success = res is not None
                elif item_type == "ticket_event":
                    success = await self.post_ticket_event(item["ticket_id"], payload, queue_on_failure=False)
                elif item_type == "ticket_create":
                    created = await self.create_ticket(payload, queue_on_failure=False)
                    success = created is not None
                elif item_type == "call_start":
                    success = await self.post_call_start(payload, queue_on_failure=False)
                elif item_type == "call_end":
                    success = await self.post_call_end(item["call_id"], payload, queue_on_failure=False)

                if success:
                    flushed_count += 1
                else:
                    self.retry_queue.append(item)
            except Exception:
                self.retry_queue.append(item)

        return flushed_count

    async def post_call_start(self, start: CallStart, queue_on_failure: bool = True) -> bool:
        url = f"{self.base_url}/v1/calls"
        body = start.model_dump(mode="json")
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 201):
                    return True
                logger.warning("ops.post_call_start returned %s: %s", resp.status_code, resp.text)
            except Exception as e:
                logger.warning("ops.post_call_start attempt %s failed: %s", attempt + 1, e)

        if queue_on_failure:
            self.retry_queue.append({"type": "call_start", "payload": start})
        return False

    async def post_turn(self, call_id: str, summary: TurnSummary, queue_on_failure: bool = True) -> bool:
        url = f"{self.base_url}/v1/calls/{call_id}/turns"
        body = summary.model_dump(mode="json")
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 201):
                    return True
                logger.warning("ops.post_turn returned %s: %s", resp.status_code, resp.text)
            except Exception as e:
                logger.warning("ops.post_turn attempt %s failed: %s", attempt + 1, e)

        if queue_on_failure:
            self.retry_queue.append({"type": "turn", "call_id": call_id, "payload": summary})
        return False

    async def post_call_end(self, call_id: str, end: CallEnd, queue_on_failure: bool = True) -> bool:
        url = f"{self.base_url}/v1/calls/{call_id}/end"
        body = end.model_dump(mode="json")
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 201):
                    return True
                logger.warning("ops.post_call_end returned %s: %s", resp.status_code, resp.text)
            except Exception as e:
                logger.warning("ops.post_call_end attempt %s failed: %s", attempt + 1, e)

        if queue_on_failure:
            self.retry_queue.append({"type": "call_end", "call_id": call_id, "payload": end})
        return False

    async def create_ticket(self, ticket: TicketCreate, queue_on_failure: bool = True) -> TicketCreated | None:
        """Create ticket with 2 retries and 2s timeout. On failure, returns None."""
        url = f"{self.base_url}/v1/tickets"
        body = ticket.model_dump(mode="json")
        for attempt in range(3):  # initial attempt + 2 retries = 3 attempts total
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 201):
                    return TICKET_CREATED_ADAPTER.validate_python(resp.json())
                logger.warning("ops.create_ticket returned status %s: %s", resp.status_code, resp.text)
            except Exception as e:
                logger.warning("ops.create_ticket attempt %s failed: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(0.1)

        if queue_on_failure:
            self.retry_queue.append({"type": "ticket_create", "payload": ticket})
        return None

    async def get_open_tickets(self, caller_number: str) -> list[TicketRef]:
        """Fetch open tickets for caller (P1 repeat-caller memory)."""
        url = f"{self.base_url}/v1/tickets"
        params = {"caller_number": caller_number, "status": "open"}
        try:
            client = await self._get_client()
            resp = await client.get(url, headers=self._headers(), params=params, timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                refs = []
                for item in items:
                    t = Ticket.model_validate(item)
                    refs.append(
                        TicketRef(
                            ticket_id=t.ticket_id,
                            type=t.type,
                            status=t.status,
                            department=t.department,
                            summary=t.summary,
                            created_at=t.created_at,
                        )
                    )
                return refs
        except Exception as e:
            logger.warning("ops.get_open_tickets failed: %s", e)
        return []

    async def post_ticket_event(
        self, ticket_id: str, event: TicketEvent, queue_on_failure: bool = True
    ) -> bool:
        url = f"{self.base_url}/v1/tickets/{ticket_id}/events"
        body = event.model_dump(mode="json")
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 201):
                    return True
            except Exception as e:
                logger.warning("ops.post_ticket_event attempt %s failed: %s", attempt + 1, e)

        if queue_on_failure:
            self.retry_queue.append({"type": "ticket_event", "ticket_id": ticket_id, "payload": event})
        return False

    async def post_sms(self, sms: SmsRequest, queue_on_failure: bool = True) -> SmsResult | None:
        url = f"{self.base_url}/v1/notifications/sms"
        body = sms.model_dump(mode="json")
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 202):
                    return SMS_RESULT_ADAPTER.validate_python(resp.json())
            except Exception as e:
                logger.warning("ops.post_sms attempt %s failed: %s", attempt + 1, e)

        if queue_on_failure:
            self.retry_queue.append({"type": "sms", "payload": sms})
        return None

    async def post_gap(self, gap: GapCreate, queue_on_failure: bool = True) -> bool:
        url = f"{self.base_url}/v1/gaps"
        body = gap.model_dump(mode="json")
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.post(url, headers=self._headers(), json=body, timeout=2.0)
                if resp.status_code in (200, 201):
                    return True
            except Exception as e:
                logger.warning("ops.post_gap attempt %s failed: %s", attempt + 1, e)

        if queue_on_failure:
            self.retry_queue.append({"type": "gap", "payload": gap})
        return False

    async def check_health(self) -> str:
        url = f"{self.base_url}/healthz"
        try:
            client = await self._get_client()
            resp = await client.get(url, timeout=1.5)
            if resp.status_code == 200:
                return "ok"
        except Exception:
            pass
        return "down"
