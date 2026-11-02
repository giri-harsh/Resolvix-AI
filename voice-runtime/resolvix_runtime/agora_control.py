from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import logging
import random
import re
import uuid
from typing import Any

from fastapi import HTTPException
import httpx

from resolvix_contracts.models import CallChannel, CallContext, CallEnd, CallOutcome, CallStart
from .agora_token import generate_convoai_token, generate_rtc_token
from .brain_loader import get_brain
from .config import RuntimeSettings
from .ops_client import OpsClient
from .sessions import get_session_registry

logger = logging.getLogger(__name__)


def _mask_secrets(text: str) -> str:
    """Mask potential secret keys in logged error strings."""
    return re.sub(r"(key|secret|token|password)[\"':=\s]+[a-zA-Z0-9_\-\.]{8,}", r"\1: [MASKED]", text, flags=re.IGNORECASE)


class AgoraController:
    """Manages Agora Conversational AI Agent sessions, tokens, and lifecycle."""

    def __init__(self, settings: RuntimeSettings, ops_client: OpsClient) -> None:
        self.settings = settings
        self.ops_client = ops_client

    def _auth_header(self) -> dict[str, str]:
        if not self.settings.agora_customer_id or not self.settings.agora_customer_secret:
            return {}
        creds = f"{self.settings.agora_customer_id}:{self.settings.agora_customer_secret}".encode()
        encoded = base64.b64encode(creds).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    async def start_web_session(self) -> dict[str, Any]:
        call_id = f"call-web-{uuid.uuid4().hex[:8]}"
        channel = f"resolvix-{uuid.uuid4().hex[:6]}"
        uid = random.randint(100000, 999999)

        registry = get_session_registry()
        session, _ = registry.get_or_create(call_id=call_id, channel=CallChannel.web)
        session.agora_channel = channel

        # 1. Initialize Brain session & get greeting
        brain = get_brain(self.settings)
        ctx = CallContext(
            call_id=call_id,
            channel=CallChannel.web,
            caller_number=None,
            started_at=session.started_at,
        )
        plan = await brain.start_call(ctx)

        # 2. Register call with ops-api
        call_start = CallStart(
            call_id=call_id,
            channel=CallChannel.web,
            caller_number=None,
            started_at=session.started_at,
            kb_version=plan.kb_version,
            lang=plan.greeting_lang,
        )
        await self.ops_client.post_call_start(call_start)

        # 3. Build AccessToken2 ("007") for browser user
        token = generate_rtc_token(
            app_id=self.settings.agora_app_id,
            app_certificate=self.settings.agora_app_certificate,
            channel_name=channel,
            uid=uid,
        )

        # 4. If in real mode or Agora App ID configured, ensure customer credentials exist and start agent
        agent_id: str | None = None
        llm_url = f"{self.settings.public_base_url}/v1/chat/completions?call_id={call_id}"
        is_real_agora = (
            self.settings.telephony_mode == "agora"
            or bool(self.settings.agora_app_id and self.settings.agora_app_id != "demo-agora-app-id")
        )

        if is_real_agora:
            if not self.settings.agora_customer_id or not self.settings.agora_customer_secret:
                logger.error(
                    "Real Agora mode is active, but AGORA_CUSTOMER_ID or AGORA_CUSTOMER_SECRET is missing. "
                    "Failing loudly to prevent empty-channel browser join."
                )
                await self._record_join_failure(call_id)
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": {
                            "code": "agora_config_missing_credentials",
                            "message": (
                                "AGORA_CUSTOMER_ID and AGORA_CUSTOMER_SECRET must be configured to start "
                                "the Agora Conversational AI agent in real mode. Set them in your environment."
                            ),
                        }
                    },
                )

            agent_id = await self._start_agora_agent(
                channel=channel,
                call_id=call_id,
                llm_url=llm_url,
                greeting=plan.greeting_text,
                user_uid=uid,
            )
            session.agora_agent_id = agent_id

        return {
            "app_id": self.settings.agora_app_id or "demo-agora-app-id",
            "channel": channel,
            "rtc_token": token,
            "uid": uid,
            "call_id": call_id,
            "agent_id": agent_id,
        }

    async def _start_agora_agent(
        self,
        channel: str,
        call_id: str,
        llm_url: str,
        greeting: str,
        user_uid: int,
    ) -> str:
        """Start Agora Conversational AI Agent (v2) in the RTC channel.

        Uses the combined RTC + RTM + ConvoAI token (v007) and fails loudly with HTTP 502 on failure.
        """
        agent_uid = 1000
        agent_token = generate_convoai_token(
            app_id=self.settings.agora_app_id,
            app_certificate=self.settings.agora_app_certificate,
            channel_name=channel,
            uid=agent_uid,
        )

        url = f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{self.settings.agora_app_id}/join"

        asr_params: dict[str, Any] = {
            "language": self.settings.asr_language,
            "model": self.settings.asr_model,
        }
        if self.settings.asr_credential_mode != "managed" and self.settings.asr_api_key:
            asr_params["key"] = self.settings.asr_api_key

        asr_payload: dict[str, Any] = {
            "vendor": self.settings.asr_vendor or "deepgram",
            "params": asr_params,
        }
        if self.settings.asr_credential_mode == "managed":
            asr_payload["credential_mode"] = "managed"

        tts_params: dict[str, Any] = {
            "model_id": self.settings.tts_model,
            "voice_id": self.settings.tts_voice_id,
        }
        if self.settings.tts_credential_mode != "managed" and self.settings.tts_api_key:
            tts_params["key"] = self.settings.tts_api_key

        tts_payload: dict[str, Any] = {
            "vendor": self.settings.tts_vendor or "elevenlabs",
            "params": tts_params,
        }
        if self.settings.tts_credential_mode == "managed":
            tts_payload["credential_mode"] = "managed"

        payload = {
            "name": f"resolvix-agent-{call_id}",
            "properties": {
                "channel": channel,
                "token": agent_token,
                "agent_rtc_uid": str(agent_uid),
                "remote_rtc_uids": [str(user_uid)],
                "enable_string_uid": False,
                "idle_timeout": 120,
                "llm": {
                    "vendor": "custom",
                    "style": "openai",
                    "url": llm_url,
                    "api_key": self.settings.shim_api_key,
                    "greeting_message": greeting,
                    "failure_message": "Sorry, I am having trouble responding right now. Please hold on or try again.",
                    "params": {
                        "model": "resolvix-brain",
                    },
                },
                "asr": asr_payload,
                "tts": tts_payload,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=self._auth_header(), json=payload)
                if resp.status_code not in (200, 201):
                    err_msg = _mask_secrets(resp.text)
                    logger.error(
                        "Agora join agent API failed with HTTP %s for call_id=%s: %s",
                        resp.status_code,
                        call_id,
                        err_msg,
                    )
                    # End ops call record so it does not remain active
                    await self._record_join_failure(call_id)
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": {
                                "code": "agent_join_failed",
                                "message": f"Agora Conversational AI agent join failed (HTTP {resp.status_code}): {err_msg}",
                            }
                        },
                    )

                data = resp.json()
                agent_id = str(data.get("agent_id") or "")
                logger.info("Successfully started Agora agent %s in channel %s for call %s", agent_id, channel, call_id)
                return agent_id

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to invoke Agora join API for call_id=%s: %s", call_id, e)
            await self._record_join_failure(call_id)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "code": "agent_join_failed",
                        "message": f"Agora Conversational AI agent join request failed: {e}",
                    }
                },
            ) from e

    async def _record_join_failure(self, call_id: str) -> None:
        """Close the call in ops and brain if agent join fails."""
        try:
            brain = get_brain(self.settings)
            await brain.end_call(call_id)
        except Exception as e:
            logger.warning("Error ending brain call for %s: %s", call_id, e)

        try:
            await self.ops_client.post_call_end(
                call_id,
                CallEnd(
                    ended_at=datetime.now(timezone.utc),
                    outcome=CallOutcome.failed,
                    ai_resolved=False,
                    turns_count=0,
                ),
            )
        except Exception as e:
            logger.warning("Error recording call failure in ops for %s: %s", call_id, e)

    async def stop_agora_agent(self, agent_id: str) -> bool:
        """Request the Conversational AI agent to leave the channel."""
        if not self.settings.agora_app_id or not agent_id:
            return False

        url = f"https://api.agora.io/api/conversational-ai-agent/v2/projects/{self.settings.agora_app_id}/agents/{agent_id}/leave"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, headers=self._auth_header())
                if resp.status_code in (200, 201, 204):
                    logger.info("Successfully stopped Agora agent %s", agent_id)
                    return True
                logger.warning("Agora leave agent API returned %s: %s", resp.status_code, resp.text)
                return False
        except Exception as e:
            logger.warning("Failed to invoke Agora leave agent API for agent %s: %s", agent_id, e)
            return False
