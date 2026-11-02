"""HTTP + WebSocket surface of the outbound service. Contract: integration/OUTBOUND_API.md.

Control endpoints (/v1/outbound-calls...) are for Ops (service key + the signed-in user's id and role). They must never be exposed on the
public edge. Only the Exotel-facing paths (/v1/webhooks/exotel/{token}, /ws/exotel/{token}) are meant to be reachable from the internet.
"""
from __future__ import annotations

import hmac
import json
import re
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from resolvix_contracts.models import ErrorBody, ErrorEnvelope

from .bridge import BridgeSession
from .models import OutboundCallRequest, OutboundError, forbidden, not_found, validation_error
from .service import ROLES_ALLOWED, OutboundService

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
USER = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,120}$")
MAX_BODY = 16 * 1024


def _envelope(exc: OutboundError) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=exc.code, message=exc.message, retryable=exc.retryable, details=exc.details))   # type: ignore[arg-type]
    headers = {"Retry-After": exc.details["retry_after_s"]} if exc.code == "rate_limited" and "retry_after_s" in exc.details else None
    return JSONResponse(status_code=exc.http_status, content=body.model_dump(mode="json"), headers=headers)


def _authenticate(request: Request, svc: OutboundService) -> tuple[str, str]:
    """Service key (constant time) + the user Ops authenticated. Ops enforces who may call; the role is re-checked here."""
    key = svc.rs.service_key or ""
    if len(key) < 16 or key == "change-me":
        raise OutboundError("internal", "The server service key is not configured.", 500)
    given = request.headers.get("x-service-key", "")
    if not given or not hmac.compare_digest(given.encode(), key.encode()):
        raise OutboundError("unauthorized", "Missing or invalid service key.", 401)
    user = request.headers.get("x-on-behalf-of", "").strip().lower()
    role = request.headers.get("x-user-role", "").strip().lower()
    if not USER.fullmatch(user):
        raise OutboundError("unauthorized", "X-On-Behalf-Of must be the signed-in user's email.", 401)
    if role not in ROLES_ALLOWED:
        raise forbidden("This role may not use outbound calling.", reason="role_not_allowed")
    return user, role


def build_router(svc: OutboundService) -> APIRouter:
    router = APIRouter()

    def owned(row_user: str, user: str, role: str) -> bool:
        return role == "admin" or row_user == user

    @router.post("/v1/outbound-calls", status_code=202)
    async def create(request: Request):
        try:
            user, role = _authenticate(request, svc)
            svc.require_enabled()
            key = request.headers.get("idempotency-key", "")
            if not IDEMPOTENCY_KEY.fullmatch(key):
                raise validation_error("An Idempotency-Key header (8-128 characters) is required.", field="Idempotency-Key")
            raw = await request.body()
            if len(raw) > MAX_BODY:
                raise validation_error("Request body too large.")
            try:
                req = OutboundCallRequest.model_validate_json(raw)
            except ValidationError as exc:
                raise validation_error("The request body is not valid.", fields=",".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())) from exc
            call, created = await svc.create_call(user, role, key, req)
            return JSONResponse(status_code=202 if created else 200, content=call, headers={"Location": f"/v1/outbound-calls/{call['call_id']}"})
        except OutboundError as exc:
            return _envelope(exc)

    @router.get("/v1/outbound-calls")
    async def history(request: Request, limit: int = 20, before: float | None = None, status: str | None = None):
        try:
            user, role = _authenticate(request, svc)
            items = svc.history(limit, before, status, None if role == "admin" else user)
            nxt = None
            if items and len(items) == max(1, min(limit, 100)):
                nxt = svc.store.get(items[-1]["call_id"])["created_at"]
            return {"items": items, "next_before": nxt}
        except OutboundError as exc:
            return _envelope(exc)

    @router.get("/v1/outbound-calls/{call_id}")
    async def get_call(call_id: str, request: Request):
        try:
            user, role = _authenticate(request, svc)
            call = svc.get(call_id)
            if not owned(call["requested_by"], user, role):
                raise not_found()                                  # staff cannot probe other users' calls
            return call
        except OutboundError as exc:
            return _envelope(exc)

    @router.post("/v1/outbound-calls/{call_id}/cancel", status_code=202)
    async def cancel(call_id: str, request: Request):
        try:
            user, role = _authenticate(request, svc)
            current = svc.get(call_id)
            if not owned(current["requested_by"], user, role):
                raise not_found()
            return await svc.cancel(call_id, user)
        except OutboundError as exc:
            return _envelope(exc)

    @router.post("/v1/webhooks/exotel/{token}")
    async def exotel_callback(token: str, request: Request):
        """Exotel status callback. Unsigned by Exotel, so authenticated by the per-call token in the path and the Sid in the body."""
        try:
            if not svc.cfg.enabled:
                raise not_found("Unknown callback.")
            raw = await request.body()
            if len(raw) > MAX_BODY:
                raise validation_error("Callback body too large.")
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype == "application/json":
                payload: Any = json.loads(raw or b"{}")
            elif ctype == "application/x-www-form-urlencoded":
                payload = dict(parse_qsl(raw.decode("utf-8", "replace")))
            else:
                raise validation_error("Unsupported callback content type.", content_type=ctype[:40])
            if not isinstance(payload, dict):
                raise validation_error("Callback body must be an object.")
            return await svc.handle_callback(token, payload)
        except json.JSONDecodeError:
            return _envelope(validation_error("Callback body is not valid JSON."))
        except OutboundError as exc:
            return _envelope(exc)

    @router.websocket("/ws/exotel/{token}")
    async def exotel_stream(websocket: WebSocket, token: str):
        row = svc.lookup_ws(token) if svc.cfg.enabled else None
        if row is None:
            await websocket.close(code=1008)                      # unknown, expired, already used or finished: never accepted
            return
        call_id = row["call_id"]
        await BridgeSession(svc, row, websocket, svc.asr_factory(call_id), svc.tts_factory(call_id)).run()

    return router
