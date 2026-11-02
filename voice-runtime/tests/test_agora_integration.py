from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import pytest
import respx
import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from resolvix_contracts.models import (
    CallChannel,
    CallContext,
    CallOutcome,
    CreateTicketAction,
    Priority,
    SendSmsAction,
    TicketCreate,
    TicketType,
    TransferAction,
)
from resolvix_runtime.agora_control import AgoraController
from resolvix_runtime.agora_token import generate_rtc_token, RtcTokenBuilder
from resolvix_runtime.app import create_app
from resolvix_runtime.config import RuntimeSettings
from resolvix_runtime.executor import ActionExecutor
from resolvix_runtime.ops_client import OpsClient
from resolvix_runtime.sessions import SessionState, get_session_registry
from resolvix_runtime.telephony.mock import MockTelephony


# --- A-6: AccessToken2 Verification ---


def test_agora_token_builder_access_token_2():
    # Valid 32-char hex UUIDs
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"
    channel = "resolvix-channel-01"
    uid = 123456

    token = generate_rtc_token(app_id, app_cert, channel, uid)
    assert token.startswith("007"), f"Expected token to start with 007, got {token[:5]}"
    assert len(token) > 100

    # Agent uid token
    agent_token = generate_rtc_token(app_id, app_cert, channel, 1000)
    assert agent_token.startswith("007")

    # Missing cert returns mock token fallback
    mock_token = generate_rtc_token(app_id, "", channel, uid)
    assert mock_token == f"mock-token-{channel}-{uid}"


# --- A-7 & A-8: Join Payload Shape, Languages, Voices, Managed Mode ---


@pytest.mark.asyncio
async def test_agora_agent_join_payload_shape_and_voices():
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"

    settings = RuntimeSettings(
        port=8000,
        agora_app_id=app_id,
        agora_app_certificate=app_cert,
        agora_customer_id="cust-123",
        agora_customer_secret="secret-456",
        asr_vendor="deepgram",
        asr_api_key="dg-key-789",
        asr_language="multi",
        asr_model="nova-3",
        asr_credential_mode="byok",
        tts_vendor="elevenlabs",
        tts_api_key="el-key-999",
        tts_model="eleven_flash_v2_5",
        tts_voice_id="pNInz6obpgDQGcFmaJgB",
        tts_credential_mode="byok",
    )

    ops_client = OpsClient(settings)
    controller = AgoraController(settings, ops_client)

    captured_payloads = []

    async with respx.mock(base_url="https://api.agora.io", assert_all_called=False) as agora_mock:
        join_route = agora_mock.post(f"/api/conversational-ai-agent/v2/projects/{app_id}/join").mock(
            side_effect=lambda req: captured_payloads.append(json.loads(req.content))
            or respx.MockResponse(200, json={"agent_id": "agent-xyz-987", "status": "RUNNING"})
        )

        agent_id = await controller._start_agora_agent(
            channel="test-channel-99",
            call_id="call-web-abc",
            llm_url="https://resolvix.example.com/v1/chat/completions?call_id=call-web-abc",
            greeting="Namaste, main Resolvix AI hoon.",
            user_uid=555666,
        )

        assert agent_id == "agent-xyz-987"
        assert len(captured_payloads) == 1
        p = captured_payloads[0]

        # Check A-7: proper properties wrapper shape
        assert p["name"] == "resolvix-agent-call-web-abc"
        props = p["properties"]
        assert props["channel"] == "test-channel-99"
        assert props["token"].startswith("007")
        assert props["agent_rtc_uid"] == "1000"
        assert props["remote_rtc_uids"] == ["555666"]

        # Check LLM: vendor custom, style openai, greeting_message, failure_message
        llm = props["llm"]
        assert llm["vendor"] == "custom"
        assert llm["style"] == "openai"
        assert llm["greeting_message"] == "Namaste, main Resolvix AI hoon."
        assert "greeting" not in llm
        assert "failure_message" in llm
        assert len(llm["failure_message"]) > 0

        # Check A-8: Deepgram Nova-3 + language multi
        asr = props["asr"]
        assert asr["vendor"] == "deepgram"
        assert asr["params"]["language"] == "multi"
        assert asr["params"]["model"] == "nova-3"
        assert asr["params"]["key"] == "dg-key-789"

        # Check A-8: ElevenLabs flash v2.5 + real voice id
        tts = props["tts"]
        assert tts["vendor"] == "elevenlabs"
        assert tts["params"]["model_id"] == "eleven_flash_v2_5"
        assert tts["params"]["voice_id"] == "pNInz6obpgDQGcFmaJgB"
        assert tts["params"]["key"] == "el-key-999"


@pytest.mark.asyncio
async def test_agora_agent_managed_mode():
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"

    settings = RuntimeSettings(
        port=8000,
        agora_app_id=app_id,
        agora_app_certificate=app_cert,
        agora_customer_id="cust-123",
        agora_customer_secret="secret-456",
        asr_credential_mode="managed",
        tts_credential_mode="managed",
    )

    ops_client = OpsClient(settings)
    controller = AgoraController(settings, ops_client)

    captured_payloads = []

    async with respx.mock(base_url="https://api.agora.io", assert_all_called=False) as agora_mock:
        agora_mock.post(f"/api/conversational-ai-agent/v2/projects/{app_id}/join").mock(
            side_effect=lambda req: captured_payloads.append(json.loads(req.content))
            or respx.MockResponse(200, json={"agent_id": "agent-managed-01", "status": "RUNNING"})
        )

        await controller._start_agora_agent(
            channel="test-channel-managed",
            call_id="call-web-managed",
            llm_url="https://resolvix.example.com/v1/chat/completions",
            greeting="Hello",
            user_uid=112233,
        )

        props = captured_payloads[0]["properties"]
        assert props["asr"]["credential_mode"] == "managed"
        assert "key" not in props["asr"]["params"]
        assert props["tts"]["credential_mode"] == "managed"
        assert "key" not in props["tts"]["params"]


# --- A-9: Agent Join Failure Handling (Error instead of warning) ---


@pytest.mark.asyncio
async def test_agora_agent_join_failure_raises_502_and_closes_ops_call():
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"

    settings = RuntimeSettings(
        port=8000,
        ops_api_url="http://localhost:8100",
        service_key="test-key",
        agora_app_id=app_id,
        agora_app_certificate=app_cert,
        agora_customer_id="cust-123",
        agora_customer_secret="secret-456",
    )

    ops_client = OpsClient(settings)
    controller = AgoraController(settings, ops_client)

    closed_calls = []

    async with respx.mock(assert_all_called=False) as mock:
        # Agora returns 400 Bad Request
        mock.post(f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{app_id}/join").mock(
            return_value=respx.MockResponse(400, json={"message": "Invalid token or UID", "code": 40001})
        )

        # Ops API records call end
        mock.post(url__regex=r"http://localhost:8100/v1/calls/.+/end").mock(
            side_effect=lambda req: closed_calls.append(json.loads(req.content))
            or respx.MockResponse(200, json={"status": "ended"})
        )

        with pytest.raises(HTTPException) as exc_info:
            await controller._start_agora_agent(
                channel="fail-channel",
                call_id="call-fail-001",
                llm_url="https://resolvix.example.com",
                greeting="Hello",
                user_uid=999,
            )

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert detail["error"]["code"] == "agent_join_failed"
        assert "400" in detail["error"]["message"]

        # Assert ops call end was posted
        assert len(closed_calls) == 1
        assert closed_calls[0]["outcome"] == "failed"


# --- Web Session End & Agent Leave API ---


@pytest.mark.asyncio
async def test_agora_agent_leave_on_session_end():
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"

    settings = RuntimeSettings(
        port=8000,
        ops_api_url="http://localhost:8100",
        service_key="test-key",
        agora_app_id=app_id,
        agora_app_certificate=app_cert,
        agora_customer_id="cust-123",
        agora_customer_secret="secret-456",
    )

    leave_called = []

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{app_id}/join").mock(
            return_value=respx.MockResponse(200, json={"agent_id": "agent-live-01", "status": "RUNNING"})
        )
        mock.post("http://localhost:8100/v1/calls").mock(
            return_value=respx.MockResponse(201, json={"status": "created"})
        )
        mock.post(url__regex=r"http://localhost:8100/v1/calls/.+/end").mock(
            return_value=respx.MockResponse(200, json={"status": "ended"})
        )
        mock.post(f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{app_id}/agents/agent-live-01/leave").mock(
            side_effect=lambda req: leave_called.append(True) or respx.MockResponse(200, json={"status": "ok"})
        )

        app = create_app(settings)
        client = TestClient(app)

        # 1. Start web session
        resp_start = client.post("/api/sessions/web")
        assert resp_start.status_code == 200
        data = resp_start.json()
        call_id = data["call_id"]
        assert data["agent_id"] == "agent-live-01"

        # 2. End web session
        resp_end = client.post("/api/sessions/web/end", json={"call_id": call_id})
        assert resp_end.status_code == 200
        assert resp_end.json()["status"] == "ended"

        # Verify Agora leave API was invoked
        assert len(leave_called) == 1


# --- Emergency Path: SMS is Non-Blocking for Transfers ---


@pytest.mark.asyncio
async def test_emergency_path_does_not_wait_for_sms_to_transfer():
    settings = RuntimeSettings(
        port=8000,
        ops_api_url="http://localhost:8100",
        service_key="test-key",
    )
    ops_client = OpsClient(settings)
    telephony = MockTelephony(transfer_success=True)
    session = SessionState(call_id="call-emerg-001", channel=CallChannel.pstn)
    executor = ActionExecutor(ops_client, telephony, settings, session)

    events_order: list[str] = []

    async with respx.mock(base_url="http://localhost:8100", assert_all_called=False) as mock:
        mock.post("/v1/tickets").mock(
            return_value=respx.MockResponse(201, json={"ticket_id": "EM-1001", "created": True})
        )
        mock.post(url__regex=r"/v1/tickets/.+/events").mock(
            return_value=respx.MockResponse(201, json={"status": "stored"})
        )

        # SMS endpoint has an artificial delay simulating Twilio/DLT network roundtrip
        async def slow_sms(request):
            events_order.append("sms_started")
            await asyncio.sleep(0.1)
            events_order.append("sms_finished")
            return respx.MockResponse(202, json={"message_id": "sms-delayed", "status": "sent", "mode": "mock"})

        mock.post("/v1/notifications/sms").mock(side_effect=slow_sms)

        # 1. Create emergency ticket
        create_act = CreateTicketAction(
            ticket=TicketCreate(
                idempotency_key="em-k",
                call_id="call-emerg-001",
                type=TicketType.EMERGENCY,
                priority=Priority.P1,
                priority_reason="medical",
                department="emergency",
                summary="Student fainted",
                collected_fields={},
                caller_number="+919000000001",
                callback_number=None,
                callback_requested=False,
                transcript_excerpt="help",
                brief="Emergency",
            )
        )
        await executor.execute(create_act, blocking=True)
        assert session.emergency is True
        assert executor.ticket_id == "EM-1001"

        # 2. Dispatch SMS with blocking=False (as emitted by safety fixture)
        sms_act = SendSmsAction(
            idempotency_key="sms-k",
            to_number="+919999999999",
            template_id="staff_alert",
            vars={"ticket_id": "{{ticket_id}}"},
            consent="staff",
        )
        await executor.execute(sms_act, blocking=False)

        # 3. Transfer immediately executes
        transfer_act = TransferAction(
            department="emergency",
            to_number="+910000000900",
            reason="medical",
            brief="Emergency",
            requires_ticket=True,
        )
        events_order.append("transfer_started")
        await executor.execute(transfer_act, blocking=True)
        events_order.append("transfer_finished")

        # Transfer must have finished while slow SMS was still running in background!
        assert "transfer_started" in events_order
        assert "transfer_finished" in events_order
        assert "sms_finished" not in events_order  # Proof transfer did not block on SMS!

        # Cleanup pending tasks - now SMS completes
        await executor.wait_pending()
        assert "sms_finished" in events_order


# --- Additional Integration Validations ---


@pytest.mark.asyncio
async def test_missing_customer_id_fails_loudly_in_real_mode():
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"

    settings = RuntimeSettings(
        port=8000,
        ops_api_url="http://localhost:8100",
        service_key="test-key",
        telephony_mode="agora",
        agora_app_id=app_id,
        agora_app_certificate=app_cert,
        agora_customer_id="",  # MISSING
        agora_customer_secret="",
    )
    ops_client = OpsClient(settings)
    controller = AgoraController(settings, ops_client)

    closed_calls = []

    async with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:8100/v1/calls").mock(
            return_value=respx.MockResponse(201, json={"status": "created"})
        )
        mock.post(url__regex=r"http://localhost:8100/v1/calls/.+/end").mock(
            side_effect=lambda req: closed_calls.append(json.loads(req.content))
            or respx.MockResponse(200, json={"status": "ended"})
        )

        with pytest.raises(HTTPException) as exc_info:
            await controller.start_web_session()

        assert exc_info.value.status_code == 500
        detail = exc_info.value.detail
        assert detail["error"]["code"] == "agora_config_missing_credentials"
        assert "AGORA_CUSTOMER_ID and AGORA_CUSTOMER_SECRET must be configured" in detail["error"]["message"]

        # Call must be marked failed in ops so no orphaned session remains
        assert len(closed_calls) == 1
        assert closed_calls[0]["outcome"] == "failed"


def test_convoai_combined_token_builder():
    from resolvix_runtime.agora_token import ConvoAITokenBuilder, generate_convoai_token

    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"
    channel = "resolvix-channel-convoai"
    agent_uid = 1000

    token = generate_convoai_token(app_id, app_cert, channel, agent_uid)
    assert token.startswith("007"), f"Expected token to start with 007, got {token[:5]}"
    assert len(token) > 100

    # Test builder directly
    raw_token = ConvoAITokenBuilder.build_token(
        app_id=app_id,
        app_certificate=app_cert,
        channel_name=channel,
        account=agent_uid,
    )
    assert raw_token.startswith("007")

    # Missing cert returns mock fallback
    mock = generate_convoai_token(app_id, "", channel, agent_uid)
    assert mock == f"mock-convoai-token-{channel}-{agent_uid}"


@pytest.mark.asyncio
async def test_speech_chunk_formatting_adds_space_between_sentences():
    from typing import AsyncGenerator
    from resolvix_contracts.models import DoneEvent, Lang, MetaEvent, Path, SpeechEvent, TurnRequest, TurnSummary
    import resolvix_runtime.brain_loader as bl
    from resolvix_runtime.shim import generate_sse_stream

    class MockBrainStreaming:
        async def handle_turn(self, req: TurnRequest) -> AsyncGenerator:
            yield MetaEvent(path=Path.faq, lang=Lang.hinglish)
            # Yield "weekends." followed by "Anything"
            yield SpeechEvent(text="weekends.")
            yield SpeechEvent(text="Anything")
            yield DoneEvent(summary=TurnSummary(
                call_id=req.call_id,
                turn_index=req.turn_index,
                path=Path.faq,
                lang=Lang.hinglish,
                user_text_redacted=req.user_text,
                assistant_text="weekends. Anything",
                created_at=datetime.now(timezone.utc),
            ))

    bl.reset_brain()
    bl._BRAIN_INSTANCE = MockBrainStreaming()

    try:
        settings = RuntimeSettings(port=8000)
        ops_client = OpsClient(settings)
        session = SessionState(call_id="call-speech-fmt")

        req = TurnRequest(
            call_id="call-speech-fmt",
            turn_index=1,
            user_text="test",
            history=[],
            dtmf=None,
            received_at=datetime.now(timezone.utc),
        )

        chunks = []
        async for sse in generate_sse_stream("call-speech-fmt", 1, req, session, settings, ops_client):
            line = sse.strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                payload = json.loads(line[6:])
                if "choices" in payload and payload["choices"]:
                    delta = payload["choices"][0]["delta"]
                    if "content" in delta:
                        chunks.append(delta["content"])

        # First chunk is "weekends.", second chunk should be formatted with leading space " Anything"
        assert chunks == ["weekends.", " Anything"]
        concatenated = "".join(chunks)
        assert concatenated == "weekends. Anything"
    finally:
        bl.reset_brain()


@pytest.mark.asyncio
async def test_emergency_sms_typed_endpoint_does_not_block_response():
    import time
    from typing import AsyncGenerator
    from resolvix_contracts.models import (
        ActionEvent,
        DoneEvent,
        Lang,
        MetaEvent,
        Path,
        Priority,
        SendSmsAction,
        SpeechEvent,
        TicketCreate,
        TicketType,
        TurnRequest,
        TurnSummary,
    )
    import resolvix_runtime.brain_loader as bl

    class MockEmergencyBrain:
        async def start_call(self, ctx: CallContext):
            from resolvix_contracts.models import CallStartPlan
            return CallStartPlan(
                kb_version="mock-kb",
                greeting_text="Hello",
                greeting_lang=Lang.hinglish,
                interruptable=False,
            )

        async def handle_turn(self, req: TurnRequest) -> AsyncGenerator:
            yield MetaEvent(path=Path.safety, lang=Lang.hinglish)
            yield ActionEvent(
                action=CreateTicketAction(
                    ticket=TicketCreate(
                        idempotency_key="em-turn-key",
                        call_id=req.call_id,
                        type=TicketType.EMERGENCY,
                        priority=Priority.P1,
                        priority_reason="medical",
                        department="emergency",
                        summary="Student emergency",
                        collected_fields={},
                        caller_number="+919000000001",
                        callback_number=None,
                        callback_requested=False,
                        transcript_excerpt="help",
                        brief="Emergency",
                    )
                ),
                blocking=True,
            )
            yield ActionEvent(
                action=SendSmsAction(
                    idempotency_key="sms-turn-key",
                    to_number="+919999999999",
                    template_id="staff_alert",
                    vars={"ticket_id": "{{ticket_id}}"},
                    consent="staff",
                ),
                blocking=False,
            )
            yield SpeechEvent(text="Emergency team alerted.")
            yield DoneEvent(summary=TurnSummary(
                call_id=req.call_id,
                turn_index=req.turn_index,
                path=Path.safety,
                lang=Lang.hinglish,
                user_text_redacted=req.user_text,
                assistant_text="Emergency team alerted.",
                created_at=datetime.now(timezone.utc),
            ))

        async def end_call(self, call_id: str) -> None:
            pass

    bl.reset_brain()
    bl._BRAIN_INSTANCE = MockEmergencyBrain()

    try:
        settings = RuntimeSettings(port=8000, ops_api_url="http://localhost:8100", service_key="test-key")
        app = create_app(settings)

        sms_completed = False

        async with respx.mock(base_url="http://localhost:8100", assert_all_called=False) as mock:
            # call start was not mocked here: respx raised, the ops client retried, and the ticket POST queued behind that (about 1 s)
            mock.post("/v1/calls").mock(return_value=respx.MockResponse(201, json={"status": "created"}))
            mock.post("/v1/tickets").mock(
                return_value=respx.MockResponse(201, json={"ticket_id": "EM-2002", "created": True})
            )
            mock.post(url__regex=r"/v1/tickets/.+/events").mock(
                return_value=respx.MockResponse(201, json={"status": "stored"})
            )
            mock.post(url__regex=r"/v1/calls/.+/turns").mock(
                return_value=respx.MockResponse(201, json={"status": "stored"})
            )
            mock.post(url__regex=r"/v1/calls/.+/end").mock(
                return_value=respx.MockResponse(200, json={"status": "ended"})
            )

            async def delayed_sms(req):
                nonlocal sms_completed
                await asyncio.sleep(0.4)
                sms_completed = True
                return respx.MockResponse(202, json={"message_id": "sms-delayed", "status": "sent", "mode": "mock"})

            mock.post("/v1/notifications/sms").mock(side_effect=delayed_sms)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                # warm-up on another call: the first request pays one-off start-up costs (imports, client creation) that depend on the machine
                await ac.post("/api/test/turn", json={"call_id": "call-typed-emerg-warmup", "text": "help"})
                await asyncio.sleep(0.5)                                  # let the warm-up call's delayed SMS finish
                sms_completed = False
                start_t = time.perf_counter()
                resp = await ac.post("/api/test/turn", json={"call_id": "call-typed-emerg-1", "text": "help"})
                elapsed = time.perf_counter() - start_t

                assert resp.status_code == 200
                assert elapsed < 0.25, f"Turn response took {elapsed:.3f}s, waited for SMS!"
                assert sms_completed is False, "SMS task should still be executing in the background!"

                resp_end = await ac.post("/api/test/end", json={"call_id": "call-typed-emerg-1"})
                assert resp_end.status_code == 200
                assert sms_completed is True, "Background SMS task must have completed when draining on end!"
    finally:
        bl.reset_brain()


@pytest.mark.asyncio
async def test_shim_call_outcome_import_and_call_end():
    """Verify shim.py imports CallOutcome and executes end-of-call teardown without NameError."""
    from typing import AsyncGenerator
    from resolvix_contracts.models import (
        CallOutcome,
        DoneEvent,
        EndCallAction,
        Lang,
        MetaEvent,
        Path,
        SpeechEvent,
        TurnRequest,
        TurnSummary,
    )
    import resolvix_runtime.brain_loader as bl
    from resolvix_runtime.shim import generate_sse_stream

    class MockEndingBrain:
        async def handle_turn(self, req: TurnRequest) -> AsyncGenerator:
            yield MetaEvent(path=Path.faq, lang=Lang.en)
            yield SpeechEvent(text="Goodbye.")
            yield DoneEvent(summary=TurnSummary(
                call_id=req.call_id,
                turn_index=req.turn_index,
                path=Path.faq,
                lang=Lang.en,
                user_text_redacted=req.user_text,
                assistant_text="Goodbye.",
                created_at=datetime.now(timezone.utc),
            ))

        async def end_call(self, call_id: str) -> None:
            pass

    bl.reset_brain()
    bl._BRAIN_INSTANCE = MockEndingBrain()

    try:
        settings = RuntimeSettings(port=8000, ops_api_url="http://localhost:8100", service_key="test-key")
        ops_client = OpsClient(settings)
        session = SessionState(call_id="call-shim-end-001")
        # Mark session as ended to trigger line 188 in shim.py
        session.is_ended = True

        closed_calls = []

        async with respx.mock(base_url="http://localhost:8100", assert_all_called=False) as mock:
            mock.post(url__regex=r"/v1/calls/.+/turns").mock(
                return_value=respx.MockResponse(201, json={"status": "stored"})
            )
            mock.post(url__regex=r"/v1/calls/.+/end").mock(
                side_effect=lambda req: closed_calls.append(json.loads(req.content))
                or respx.MockResponse(200, json={"status": "ended"})
            )

            req = TurnRequest(
                call_id="call-shim-end-001",
                turn_index=1,
                user_text="bye",
                history=[],
                dtmf=None,
                received_at=datetime.now(timezone.utc),
            )

            chunks = []
            async for sse in generate_sse_stream("call-shim-end-001", 1, req, session, settings, ops_client):
                chunks.append(sse)

            assert len(chunks) > 0
            # Verify call end was posted with CallOutcome
            assert len(closed_calls) == 1
            assert closed_calls[0]["outcome"] == CallOutcome.answered
            assert closed_calls[0]["ai_resolved"] is True
    finally:
        bl.reset_brain()


@pytest.mark.asyncio
async def test_speech_and_injected_speech_streaming_spacing():
    """Verify sentence chunk spacing between SpeechEvent and Action injected speech."""
    from typing import AsyncGenerator
    from resolvix_contracts.models import (
        ActionEvent,
        DoneEvent,
        Lang,
        MetaEvent,
        Path,
        SpeechEvent,
        TransferAction,
        TurnRequest,
        TurnSummary,
    )
    import resolvix_runtime.brain_loader as bl
    from resolvix_runtime.shim import generate_sse_stream

    class MockMultiChunkBrain:
        async def handle_turn(self, req: TurnRequest) -> AsyncGenerator:
            yield MetaEvent(path=Path.faq, lang=Lang.en)
            # 1. Speech chunk 1
            yield SpeechEvent(text="Please hold.")
            # 2. Action that generates injected speech
            yield ActionEvent(
                action=TransferAction(
                    department="admissions",
                    to_number="+910000000001",
                    reason="inquiry",
                    brief="transfer",
                    requires_ticket=False,
                ),
                blocking=True,
            )
            # 3. Speech chunk 2
            yield SpeechEvent(text="How else can I help?")
            yield DoneEvent(summary=TurnSummary(
                call_id=req.call_id,
                turn_index=req.turn_index,
                path=Path.faq,
                lang=Lang.en,
                user_text_redacted=req.user_text,
                assistant_text="Please hold. Transfer failed. How else can I help?",
                created_at=datetime.now(timezone.utc),
            ))

    bl.reset_brain()
    bl._BRAIN_INSTANCE = MockMultiChunkBrain()

    try:
        settings = RuntimeSettings(
            port=8000,
            ops_api_url="http://localhost:8100",
            service_key="test-key",
            telephony_transfer_success=False,  # Triggers transfer_failed injected speech
        )
        ops_client = OpsClient(settings)
        session = SessionState(call_id="call-spacing-test", channel=CallChannel.pstn)

        req = TurnRequest(
            call_id="call-spacing-test",
            turn_index=1,
            user_text="transfer me",
            history=[],
            dtmf=None,
            received_at=datetime.now(timezone.utc),
        )

        deltas = []
        async with respx.mock(base_url="http://localhost:8100", assert_all_called=False) as mock:
            mock.post(url__regex=r"/v1/tickets/.+/events").mock(
                return_value=respx.MockResponse(201, json={"status": "stored"})
            )
            mock.post(url__regex=r"/v1/calls/.+/turns").mock(
                return_value=respx.MockResponse(201, json={"status": "stored"})
            )

            async for sse in generate_sse_stream("call-spacing-test", 1, req, session, settings, ops_client):
                line = sse.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    payload = json.loads(line[6:])
                    if "choices" in payload and payload["choices"]:
                        delta = payload["choices"][0]["delta"]
                        if "content" in delta:
                            deltas.append(delta["content"])

        # First chunk: "Please hold."
        # Injected speech: " " + fail_line
        # Third chunk: " How else can I help?"
        assert len(deltas) >= 3
        concatenated = "".join(deltas)
        assert "Please hold." in concatenated
        assert "How else can I help?" in concatenated
        # Must not contain concatenated words without spaces (e.g. "hold.Transfer" or "line.How")
        assert "hold.Transfer" not in concatenated
        assert "line.How" not in concatenated
        assert "hold. " in concatenated or "hold. Transfer" in concatenated or "hold. Main" in concatenated
    finally:
        bl.reset_brain()


@pytest.mark.asyncio
async def test_english_only_deepgram_and_elevenlabs_config_defaults():
    """Verify Phase 1.1 English-only Deepgram Nova-3 and ElevenLabs flash configuration."""
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    app_cert = "5cfd2fd1755d40ecb729f39468aa3dbb"

    # Default settings without explicit overrides
    settings = RuntimeSettings(
        port=8000,
        agora_app_id=app_id,
        agora_app_certificate=app_cert,
        agora_customer_id="cust-123",
        agora_customer_secret="secret-456",
    )

    # 1. Check default settings in config.py
    assert settings.asr_vendor == "deepgram"
    assert settings.asr_model == "nova-3"
    assert settings.asr_language == "en"  # English-only default for Phase 1.1
    assert settings.asr_credential_mode == "byok"
    assert settings.tts_vendor == "elevenlabs"
    assert settings.tts_model == "eleven_flash_v2_5"
    assert settings.tts_voice_id == "pNInz6obpgDQGcFmaJgB"
    assert settings.tts_credential_mode == "byok"

    # 2. Check generated join payload uses English-only ASR language "en"
    ops_client = OpsClient(settings)
    controller = AgoraController(settings, ops_client)

    captured_payloads = []

    async with respx.mock(base_url="https://api.agora.io", assert_all_called=False) as agora_mock:
        agora_mock.post(f"/api/conversational-ai-agent/v2/projects/{app_id}/join").mock(
            side_effect=lambda req: captured_payloads.append(json.loads(req.content))
            or respx.MockResponse(200, json={"agent_id": "agent-en-001", "status": "RUNNING"})
        )

        agent_id = await controller._start_agora_agent(
            channel="test-channel-en",
            call_id="call-web-en",
            llm_url="https://resolvix.example.com/v1/chat/completions",
            greeting="Hello! Welcome to Resolvix support.",
            user_uid=12345,
        )

        assert agent_id == "agent-en-001"
        assert len(captured_payloads) == 1
        props = captured_payloads[0]["properties"]
        assert props["asr"]["params"]["language"] == "en"
        assert props["asr"]["params"]["model"] == "nova-3"
        assert props["tts"]["params"]["model_id"] == "eleven_flash_v2_5"
        assert props["tts"]["params"]["voice_id"] == "pNInz6obpgDQGcFmaJgB"




@pytest.mark.asyncio
async def test_agora_join_payload_managed_deepgram_and_minimax_has_no_keys():
    app_id = "970ca35de60c4464bb03a650e680fb9d"
    settings = RuntimeSettings(
        port=8000, agora_app_id=app_id, agora_app_certificate="5cfd2fd1755d40ecb729f39468aa3dbb",
        agora_customer_id="cust-123", agora_customer_secret="secret-456",
        asr_vendor="deepgram", asr_language="hi", asr_model="nova-3", asr_credential_mode="managed", asr_api_key="must-not-be-sent",
        tts_vendor="minimax", tts_model="speech-2.8-turbo", tts_voice_id="Hindi_female_1_v1", tts_credential_mode="managed",
        tts_api_key="must-not-be-sent", tts_group_id="must-not-be-sent", tts_language_boost="Hindi",
    )
    controller = AgoraController(settings, OpsClient(settings))
    captured = []
    async with respx.mock(base_url="https://api.agora.io", assert_all_called=False) as agora_mock:
        agora_mock.post(f"/api/conversational-ai-agent/v2/projects/{app_id}/join").mock(
            side_effect=lambda req: captured.append(json.loads(req.content)) or respx.MockResponse(200, json={"agent_id": "a1", "status": "RUNNING"}))
        await controller._start_agora_agent("ch", "call-web-x", "https://t.example/v1/chat/completions?call_id=call-web-x", "Namaste", 777)
    props = captured[0]["properties"]
    assert props["asr"] == {"vendor": "deepgram", "credential_mode": "managed",
                            "params": {"language": "hi", "model": "nova-3", "url": "wss://api.deepgram.com/v1/listen"}}
    tts = props["tts"]
    assert tts["vendor"] == "minimax" and tts["credential_mode"] == "managed"
    assert tts["params"]["model"] == "speech-2.8-turbo" and tts["params"]["voice_setting"]["voice_id"] == "Hindi_female_1_v1"
    assert tts["params"]["language_boost"] == "Hindi" and tts["params"]["audio_setting"]["sample_rate"] == 44100
    assert "key" not in tts["params"] and "group_id" not in tts["params"] and "key" not in props["asr"]["params"]
    assert props["idle_timeout"] == 120
