from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import app as bridge_app
from utils.email_core import ensure_list
from utils.email_display import render_email_markdown

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


@router.post("/show-draft")
async def show_draft(request: Request) -> dict[str, Any]:
    """Render a draft preview and send it to the owner notification target."""
    if not bridge_app.SETTINGS.resend_api_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY is not configured")
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    payload = raw.get("payload") or {}
    draft_id = str(raw.get("draft_id") or "").strip() or None
    title = str(raw.get("title") or "请确认是否发送以下邮件：").strip()
    footer = str(raw.get("footer") or "").strip() or None

    notice = render_email_markdown(
        payload,
        title=title,
        domain=bridge_app.SETTINGS.resend_domain,
        draft_id=draft_id,
        footer=footer,
        show_attachments=False,
        notice_limit=3800,
    )
    await bridge_app.notify_telegram(notice, email_id=None, attachment_paths=[])
    return {"ok": True}
