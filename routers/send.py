from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import app as bridge_app
from utils.email_core import ensure_list

router = APIRouter()


def request_bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-resend-bridge-secret", "").strip()


def verify_send_authorization(request: Request) -> None:
    token = request_bearer_token(request)
    if not token or not hmac.compare_digest(token, bridge_app.SETTINGS.bridge_send_secret):
        raise HTTPException(status_code=401, detail="invalid send authorization")


@router.post("/send")
async def send_email(request: Request) -> dict[str, Any]:
    if not bridge_app.SETTINGS.resend_api_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY is not configured")
    verify_send_authorization(request)
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
