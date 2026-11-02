"""Outbound phone calling (mock-only build): Exotel Connect + Voicebot bridge design, no real provider, no network.

    from resolvix_runtime.outbound import build_outbound
    svc, router = build_outbound(runtime_settings, ops_client)
"""
from __future__ import annotations

from ..config import RuntimeSettings
from ..ops_client import OpsClient
from .api import build_router
from .service import OutboundService
from .settings import OutboundSettings


def build_outbound(runtime_settings: RuntimeSettings, ops_client: OpsClient, settings: OutboundSettings | None = None, **kwargs):
    """-> (OutboundService, APIRouter). Disabled (and storage-free) unless OUTBOUND_ENABLED=true."""
    svc = OutboundService(runtime_settings, settings or OutboundSettings.from_env(), ops_client, **kwargs)
    return svc, build_router(svc)


__all__ = ["OutboundService", "OutboundSettings", "build_outbound"]
