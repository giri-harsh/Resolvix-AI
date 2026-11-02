from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter

from resolvix_contracts import CONTRACT_VERSION
from resolvix_contracts.models import (
    BrainEvent,
    CallChannel,
    CallContext,
    CallEnd,
    CallOutcome,
    CallStart,
    DoneEvent,
    ErrorBody,
    ErrorEnvelope,
    KBSource,
    KBVersionInfo,
    SpeechEvent,
    TurnRequest,
)
from .agora_control import AgoraController
from .brain_loader import get_brain
from .config import RuntimeSettings
from .executor import ActionExecutor
from .ops_client import OpsClient
from .sessions import SessionState, get_session_registry
from .shim import init_shim_router
from .telephony import get_telephony_adapter
from .voice_api import VoiceWebApi

logger = logging.getLogger("resolvix_runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

STATIC_DIR = Path(__file__).parent / "static"


class TestTurnRequest(BaseModel):
    call_id: str | None = None
    text: str
    caller_number: str | None = None


class CallEndRequest(BaseModel):
    call_id: str


class TestTurnResponse(BaseModel):
    call_id: str
    events: list[dict[str, Any]]
    spoken_text: str
    ticket_id: str | None = None   # set when a ticket was created in this turn (voice-web R-4)


def create_app(settings: RuntimeSettings | None = None) -> FastAPI:
    if settings is None:
        settings = RuntimeSettings.from_env()

    ops_client = OpsClient(settings)
    agora_controller = AgoraController(settings, ops_client)
    voice_web = VoiceWebApi(settings, ops_client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ops_client.start()
        await voice_web.start()
        brain = get_brain(settings)
        if hasattr(brain, "ready"):
            try:
                await brain.ready()
            except Exception as e:
                logger.error("Brain ready() error: %s", e)
        logger.info("Resolvix voice-runtime started on port %s", settings.port)
        yield
        await voice_web.stop()
        if hasattr(brain, "aclose"):
            try:
                await brain.aclose()
            except Exception:
                pass
        await ops_client.close()
        logger.info("Resolvix voice-runtime shutdown complete.")

    app = FastAPI(
        title="Resolvix Voice Runtime (Member A)",
        version=CONTRACT_VERSION,
        lifespan=lifespan,
    )

    # 1. Mount OpenAI-compatible chat completions router
    shim_router = init_shim_router(settings, ops_client)
    app.include_router(shim_router)

    # CORS for the page served from another origin (voice-web): /api/test/*, /healthz and /api/voice share one allow-list
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.voice_web_cors_origins.split(",") if o.strip()],
                       allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "Accept"], max_age=600)

    # 1b. Browser voice chat API (voice-web, "Plan B"); contract in integration/VOICE_WEB_API.md
    app.mount("/api/voice", voice_web.build_app())

    # 2. Health check endpoint
    @app.get("/healthz")
    async def health():
        brain = get_brain(settings)
        ops_health = await ops_client.check_health()
        agora_health = "mock" if settings.telephony_mode == "mock" or not settings.agora_app_id else "ok"
        brain_h = brain.health()
        overall_status = "ok" if brain_h.get("status") == "ok" and ops_health == "ok" else "degraded"

        return {
            "status": overall_status,
            "brain": brain_h,
            "ops_api": ops_health,
            "agora": agora_health,
            "contract_version": CONTRACT_VERSION,
        }

    # 3. Web session start & end (Agora tokens + AI agent join / leave)
    @app.post("/api/sessions/web")
    async def sessions_web():
        session_info = await agora_controller.start_web_session()
        return session_info

    @app.post("/api/sessions/web/end")
    async def sessions_web_end(payload: CallEndRequest):
        call_id = payload.call_id
        registry = get_session_registry()
        session = registry.get(call_id)
        if session:
            session.is_ended = True
            await session.drain_background_tasks()
            if session.agora_agent_id:
                await agora_controller.stop_agora_agent(session.agora_agent_id)
            brain = get_brain(settings)
            await brain.end_call(call_id)
            await ops_client.post_call_end(
                call_id,
                CallEnd(
                    ended_at=datetime.now(timezone.utc),
                    outcome=session.derive_outcome(),
                    ai_resolved=len(session.created_tickets) == 0,
                    turns_count=session.turn_count,
                ),
            )
        return {"status": "ended", "call_id": call_id}

    # 4. Typed test turn & end endpoints (FR-A3)
    @app.post("/api/test/turn", response_model=TestTurnResponse)
    async def test_turn(payload: TestTurnRequest):
        call_id = payload.call_id or f"call-test-{uuid.uuid4().hex[:8]}"
        caller_number = payload.caller_number
        registry = get_session_registry()
        session, is_new = registry.get_or_create(call_id=call_id, channel=CallChannel.test, caller_number=caller_number)
        if caller_number and not session.caller_number:
            session.caller_number = caller_number

        brain = get_brain(settings)
        if is_new:
            ctx = CallContext(
                call_id=call_id,
                channel=CallChannel.test,
                caller_number=caller_number,
                started_at=session.started_at,
            )
            plan = await brain.start_call(ctx)
            call_start = CallStart(
                call_id=call_id,
                channel=CallChannel.test,
                caller_number=caller_number,
                started_at=session.started_at,
                kb_version=plan.kb_version,
                lang=plan.greeting_lang,
            )
            asyncio.create_task(ops_client.post_call_start(call_start))

        turn_index = session.next_turn_index()
        req = TurnRequest(
            call_id=call_id,
            turn_index=turn_index,
            user_text=payload.text,
            history=[],
            dtmf=None,
            received_at=datetime.now(timezone.utc),
        )

        telephony = get_telephony_adapter(settings)
        executor = ActionExecutor(ops_client, telephony, settings, session)

        substituted_events: list[dict[str, Any]] = []
        spoken_pieces: list[str] = []
        current_lang = "hinglish"

        async for event in brain.handle_turn(req):
            if event.type == "meta":
                current_lang = event.lang.value if hasattr(event.lang, "value") else str(event.lang)
                substituted_events.append(event.model_dump(mode="json"))

            elif event.type == "action":
                await executor.execute(event.action, lang=current_lang, blocking=getattr(event, "blocking", True))
                substituted_events.append(event.model_dump(mode="json"))

                while executor.injected_speech:
                    inj = executor.injected_speech.pop(0)
                    spoken_pieces.append(inj)

            elif event.type == "speech":
                assert isinstance(event, SpeechEvent)
                processed = executor.process_speech_text(event.text, lang=current_lang)
                if processed:
                    spoken_pieces.append(processed)
                    substituted_events.append({"type": "speech", "text": processed})

            elif event.type == "done":
                assert isinstance(event, DoneEvent)
                # Background tasks like SMS are tracked in session.background_tasks and continue safely
                asyncio.create_task(ops_client.post_turn(call_id, event.summary))
                substituted_events.append(event.model_dump(mode="json"))

        return TestTurnResponse(
            call_id=call_id,
            events=substituted_events,
            spoken_text=" ".join(spoken_pieces).strip(),
            ticket_id=executor.ticket_id,
        )

    @app.post("/api/test/end")
    async def test_end(payload: CallEndRequest):
        call_id = payload.call_id
        registry = get_session_registry()
        session = registry.get(call_id)
        if session is None:
            err = ErrorEnvelope(error=ErrorBody(code="not_found", message="Unknown or already ended call.", retryable=False))
            return JSONResponse(status_code=404, content=err.model_dump(mode="json"))
        session.is_ended = True
        await session.drain_background_tasks()
        brain = get_brain(settings)
        await brain.end_call(call_id)
        outcome = CallOutcome.abandoned if session.turn_count == 0 else session.derive_outcome()
        await ops_client.post_call_end(
            call_id,
            CallEnd(
                ended_at=datetime.now(timezone.utc),
                outcome=outcome,
                ai_resolved=outcome == CallOutcome.answered,
                turns_count=session.turn_count,
            ),
        )
        registry.remove(call_id)
        return {"ok": True, "status": "ended", "call_id": call_id, "outcome": outcome.value}

    # 5. KB reload passthrough (FR-A1 / 4.6)
    @app.post("/admin/kb/reload")
    async def reload_kb(source: KBSource, x_service_key: str | None = Header(None)):
        if not x_service_key or x_service_key.strip() != settings.service_key:
            err = ErrorEnvelope(
                error=ErrorBody(
                    code="unauthorized",
                    message="Missing or invalid X-Service-Key",
                    retryable=False,
                )
            )
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=err.model_dump(mode="json"))

        brain = get_brain(settings)
        try:
            info = await brain.reload_kb(source)
            return info.model_dump(mode="json")
        except Exception as e:
            logger.error("KB reload compilation failed: %s", e)
            err = ErrorEnvelope(
                error=ErrorBody(
                    code="validation_error",
                    message=f"KB reload compilation failed: {e}",
                    retryable=False,
                    details={"error": str(e)},
                )
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content=err.model_dump(mode="json"),
            )

    # 6. Static page routes
    @app.get("/test", response_class=HTMLResponse)
    async def serve_test_page():
        html_file = STATIC_DIR / "test.html"
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))

    @app.get("/call", response_class=HTMLResponse)
    async def serve_call_page():
        html_file = STATIC_DIR / "call.html"
        return HTMLResponse(content=html_file.read_text(encoding="utf-8"))

    @app.get("/", response_class=RedirectResponse)
    async def root():
        return RedirectResponse(url="/test")

    return app


app = create_app()
