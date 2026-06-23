from __future__ import annotations

from typing import Any

from fastapi import APIRouter

import app as bridge_app

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    database = bridge_app.db_health()
    settings = bridge_app.SETTINGS
    return {
        "ok": database.get("ok") is True,
        "resend_api_key_configured": bool(settings.resend_api_key),
        "resend_webhook_secret_configured": bool(settings.resend_webhook_secret),
        "hermes_binary_available": settings.hermes_send_bin.exists(),
        "database": database,
        "inbound_address": settings.inbound_address,
        "owner_address": settings.owner_address,
    }
