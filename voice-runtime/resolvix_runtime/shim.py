from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from resolvix_contracts.models import (
    CallChannel,
    CallContext,
    CallEnd,
    CallOutcome,
    CallStart,
    ChatMessage,
    DoneEvent,
    MetaEvent,
    Priority,
    SpeechEvent,
    TicketCreate,
    TicketType,
    TurnRequest,
)
from .brain_loader import get_brain
from .config import RuntimeSettings
from .executor import ActionExecutor
from .ops_client import OpsClient
from .sessions import SessionState, get_session_registry
from .telephony import get_telephony_adapter

logger = logging.getLogger(__name__)



def parse_call_id(query_call_id: str | None, messages: list[dict[str, Any]]) -> str:
    """Resolve call_id from query param, system message, or generated uuid."""
    if query_call_id:
        return query_call_id.strip()

    for msg in messages:
        if msg.get("role") == "system":
            content = str(msg.get("content", ""))
            if "RESOLVIX_CALL_ID=" in content:
                for line in content.splitlines():
                    if line.startswith("RESOLVIX_CALL_ID="):
                        val = line.split("=", 1)[1].strip()
                        if val:
                            return val

    new_id = f"call-gen-{uuid.uuid4().hex[:8]}"
    logger.warning("No call_id provided in query or system message. Generated fallback call_id: %s", new_id)
    return new_id


def format_speech_chunk(text: str, is_first_speech: bool, last_speech_text: str) -> tuple[str, bool, str]:
    """Ensure clean sentence chunk spacing across streamed Brain and injected speech events."""
    if not text:
        return "", is_first_speech, last_speech_text

    if is_first_speech:
        cleaned = text.lstrip()
        return cleaned, False, cleaned

    if last_speech_text and not last_speech_text[-1].isspace() and not text.startswith((" ", "\n", "\t")):
        formatted = " " + text
    else:
        formatted = text
    return formatted, False, formatted


async def generate_sse_stream(
    call_id: str,
    turn_index: int,
    req: TurnRequest,
    session: SessionState,
    settings: RuntimeSettings,
    ops_client: OpsClient,
) -> AsyncIterator[str]:
    """Stream Agora custom-LLM SSE response chunks."""
    chunk_id = f"resolvix-{call_id}-{turn_index}"
    created_ts = int(time.time())
    brain = get_brain(settings)
    telephony = get_telephony_adapter(settings)
    executor = ActionExecutor(ops_client, telephony, settings, session)

    current_lang = "hinglish"
    is_first_speech = True
    last_speech_text = ""

    agen = brain.handle_turn(req)
    try:
        async for event in agen:
            if event.type == "meta":
                assert isinstance(event, MetaEvent)
                session.active_turn_interruptable = event.interruptable
                current_lang = event.lang.value if hasattr(event.lang, "value") else str(event.lang)
                meta_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.custom_metadata",
                    "created": created_ts,
                    "model": "resolvix-brain",
                    "choices": [],
                    "metadata": {"interruptable": event.interruptable},
                }
                yield f"data: {json.dumps(meta_chunk, separators=(',', ':'))}\n\n"

            elif event.type == "action":
                await executor.execute(event.action, lang=current_lang, blocking=getattr(event, "blocking", True))

                # Stream any extra injected speech produced by this action (e.g. transfer failed line)
                while executor.injected_speech:
                    injected_text = executor.injected_speech.pop(0)
                    if not injected_text or not injected_text.strip():
                        continue
                    was_first = is_first_speech
                    injected_text, is_first_speech, last_speech_text = format_speech_chunk(
                        injected_text, is_first_speech, last_speech_text
                    )
                    delta = {"role": "assistant", "content": injected_text} if was_first else {"content": injected_text}
                    speech_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": "resolvix-brain",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(speech_chunk, separators=(',', ':'))}\n\n"

            elif event.type == "speech":
                assert isinstance(event, SpeechEvent)
                processed_text = executor.process_speech_text(event.text, lang=current_lang)
                if processed_text and processed_text.strip():
                    was_first = is_first_speech
                    processed_text, is_first_speech, last_speech_text = format_speech_chunk(
                        processed_text, is_first_speech, last_speech_text
                    )
                    delta = {"role": "assistant", "content": processed_text} if was_first else {"content": processed_text}
                    speech_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": "resolvix-brain",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(speech_chunk, separators=(',', ':'))}\n\n"

            elif event.type == "done":
                assert isinstance(event, DoneEvent)
                # Post turn summary in background non-blocking without delaying response stream
                asyncio.create_task(ops_client.post_turn(call_id, event.summary))

    except asyncio.CancelledError:
        logger.info("SSE stream cancelled by barge-in for call_id=%s turn=%s", call_id, turn_index)
        raise
    except Exception as e:
        logger.error("Unhandled error during handle_turn for call_id=%s: %s", call_id, e, exc_info=True)
        # Never fail HTTP after streaming starts. Stream fallback line and log SYSTEM_FAILURE ticket.
        fallback_text = settings.get_runtime_line("fallback", current_lang)
        was_first = is_first_speech
        fallback_text, is_first_speech, last_speech_text = format_speech_chunk(
            fallback_text, is_first_speech, last_speech_text
        )
        err_delta = {"role": "assistant", "content": fallback_text} if was_first else {"content": fallback_text}
        err_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": "resolvix-brain",
            "choices": [{"index": 0, "delta": err_delta, "finish_reason": None}],
        }
        yield f"data: {json.dumps(err_chunk, separators=(',', ':'))}\n\n"

        # Create system failure ticket via ops
        asyncio.create_task(
            ops_client.create_ticket(
                TicketCreate(
                    idempotency_key=f"{call_id}:{turn_index}:SYSTEM_FAILURE",
                    call_id=call_id,
                    type=TicketType.SYSTEM_FAILURE,
                    priority=Priority.P1,
                    priority_reason="System error during speech streaming",
                    department="helpdesk",
                    summary=f"Voice runtime exception: {str(e)[:200]}",
                )
            )
        )
    finally:
        if hasattr(agen, "aclose"):
            try:
                await agen.aclose()
            except Exception:
                pass

    # Finish chunk
    finish_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created_ts,
        "model": "resolvix-brain",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(finish_chunk, separators=(',', ':'))}\n\n"
    yield "data: [DONE]\n\n"

    # If call was marked ended, handle call teardown
    if session.is_ended:
        outcome = session.derive_outcome()
        end_call_payload = CallEnd(
            ended_at=datetime.now(timezone.utc),
            outcome=outcome,
            ai_resolved=(outcome == CallOutcome.answered),
            turns_count=session.turn_count,
        )
        await ops_client.post_call_end(call_id, end_call_payload)
        await brain.end_call(call_id)


def init_shim_router(settings: RuntimeSettings, ops_client: OpsClient) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        call_id: str | None = Query(None),
        authorization: str | None = Header(None),
    ):
        # 1. Authenticate Bearer token
        expected_token = f"Bearer {settings.shim_api_key}"
        if not authorization or authorization.strip() != expected_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing Bearer token",
            )

        # 2. Parse request body
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

        if not body.get("stream", False):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="stream must be true",
            )

        messages = body.get("messages", [])
        resolved_call_id = parse_call_id(call_id, messages)

        # 3. Session management
        registry = get_session_registry()
        session, is_new = registry.get_or_create(
            call_id=resolved_call_id,
            channel=CallChannel.web,
        )

        brain = get_brain(settings)
        if is_new:
            ctx = CallContext(
                call_id=resolved_call_id,
                channel=CallChannel.web,
                caller_number=session.caller_number,
                started_at=session.started_at,
            )
            # Fetch open tickets if caller_number is known (P1 repeat caller)
            if session.caller_number:
                ctx.open_tickets = await ops_client.get_open_tickets(session.caller_number)

            plan = await brain.start_call(ctx)
            call_start = CallStart(
                call_id=resolved_call_id,
                channel=CallChannel.web,
                caller_number=session.caller_number,
                started_at=session.started_at,
                kb_version=plan.kb_version,
                lang=plan.greeting_lang,
            )
            asyncio.create_task(ops_client.post_call_start(call_start))

        # 4. Extract turn inputs
        turn_index = session.next_turn_index()
        user_text = ""
        history: list[ChatMessage] = []

        # Find the last user message
        last_user_idx = -1
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                last_user_idx = idx
                user_text = str(messages[idx].get("content", ""))
                break

        # Earlier messages become history
        for idx, m in enumerate(messages):
            if idx == last_user_idx:
                continue
            r = m.get("role")
            if r in ("system", "user", "assistant"):
                history.append(ChatMessage(role=r, content=str(m.get("content", ""))))

        turn_req = TurnRequest(
            call_id=resolved_call_id,
            turn_index=turn_index,
            user_text=user_text,
            history=history,
            dtmf=None,
            received_at=datetime.now(timezone.utc),
        )

        # 5. Set up stream and barge-in tracking
        stream_gen = generate_sse_stream(
            call_id=resolved_call_id,
            turn_index=turn_index,
            req=turn_req,
            session=session,
            settings=settings,
            ops_client=ops_client,
        )

        # To support barge-in task cancellation, we register a generator wrapper task
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def pump():
            try:
                async for chunk in stream_gen:
                    await queue.put(chunk)
            except asyncio.CancelledError:
                logger.info("Pump task cancelled for call_id=%s", resolved_call_id)
            finally:
                await queue.put(None)

        pump_task = asyncio.create_task(pump())
        registry.register_task(resolved_call_id, pump_task)

        async def response_generator():
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                if not pump_task.done():
                    pump_task.cancel()

        return StreamingResponse(
            response_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
