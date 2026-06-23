from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import app as bridge_app
from utils.email_core import ensure_list

router = APIRouter()


@router.post("/send")
async def send_email(request: Request) -> dict[str, Any]:
    if not bridge_app.SETTINGS.resend_api_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY is not configured")
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    payload = bridge_app.normalize_send_payload(
        raw, allow_bot_sender=bridge_app._is_authorized_bot_reply(raw)
    )
    email_id = (
        str(raw.get("email_id") or raw.get("auto_reply_email_id") or "").strip() or None
    )
    outbound_id, resend_id = await bridge_app.send_resend_email(payload, email_id=email_id)
    return {
        "ok": True,
        "outbound_id": outbound_id,
        "resend_id": resend_id,
        "from": payload["from"],
        "to": payload["to"],
        "cc": payload.get("cc", []),
        "subject": payload["subject"],
        "attachments": [
            bridge_app.outbound_attachment_audit_metadata(item)
            for item in ensure_list(payload.get("attachments"))
            if isinstance(item, dict)
        ],
    }
