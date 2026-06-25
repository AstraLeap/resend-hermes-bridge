from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import app as bridge_app
from utils.email_core import ensure_list

router = APIRouter()


def _clean_optional_notification_target(value: Any) -> str | None:
    target = str(value or "").strip()
    if not target:
        return None
    if "\r" in target or "\n" in target or len(target) > 500:
        raise HTTPException(status_code=400, detail="target is invalid")
    return target


@router.post("/send")
async def send_email(request: Request) -> dict[str, Any]:
    if not bridge_app.SETTINGS.resend_api_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY is not configured")
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="JSON object body is required")
    authorized_bot_reply = bridge_app._is_authorized_bot_reply(raw)
    payload = bridge_app.normalize_send_payload(
        raw, allow_bot_sender=authorized_bot_reply
    )
    draft_id = None
    if not authorized_bot_reply:
        draft_id = bridge_app.reserve_mcp_draft_send(raw)
    email_id = (
        str(raw.get("email_id") or raw.get("auto_reply_email_id") or "").strip() or None
    )
    try:
        outbound_id, resend_id = await bridge_app.send_resend_email(payload, email_id=email_id)
    except Exception as exc:
        if draft_id:
            try:
                bridge_app.mark_mcp_draft_send_failed(draft_id, exc)
            except Exception:
                bridge_app.LOGGER.exception("could not mark MCP draft send failure")
        raise
    result = {
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
    if draft_id:
        try:
            bridge_app.mark_mcp_draft_sent(draft_id, result)
        except Exception:
            bridge_app.LOGGER.exception("could not mark MCP draft sent")
    return result


@router.post("/show-draft")
async def show_draft(request: Request) -> dict[str, Any]:
    """Render a draft preview and send it to the requested notification target."""
    if not bridge_app.SETTINGS.resend_api_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY is not configured")
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="JSON object body is required")

    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    draft_id = str(raw.get("draft_id") or "").strip() or None
    title = str(raw.get("title") or "请确认是否发送以下邮件：").strip()
    footer = str(raw.get("footer") or "").strip() or None
    target = _clean_optional_notification_target(raw.get("target"))

    bridge_app.LOGGER.info(
        "show-draft called draft_id=%s target=%s default=%s",
        draft_id,
        target,
        bridge_app.SETTINGS.notification_target,
    )

    await bridge_app.send_email_display_notification(
        payload,
        title=title,
        domain=bridge_app.SETTINGS.resend_domain,
        draft_id=draft_id,
        footer=footer,
        show_attachments=False,
        notice_limit=3800,
        target=target,
    )
    return {"ok": True}
