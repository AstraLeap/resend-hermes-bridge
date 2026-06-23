from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from svix.webhooks import Webhook

import app as bridge_app
from db.state import StepStatus

router = APIRouter()


def verify_resend_webhook(raw_body: bytes, request: Request) -> dict[str, Any]:
    headers = {
        "svix-id": request.headers.get("svix-id", ""),
        "svix-timestamp": request.headers.get("svix-timestamp", ""),
        "svix-signature": request.headers.get("svix-signature", ""),
    }
    try:
        verified = Webhook(bridge_app.SETTINGS.resend_webhook_secret).verify(
            raw_body.decode("utf-8"),
            headers,
        )
    except Exception as exc:
        bridge_app.LOGGER.warning("invalid Resend webhook signature: %s", exc)
        raise HTTPException(
            status_code=400, detail="invalid webhook signature"
        ) from exc

    if isinstance(verified, dict):
        return verified
    return json.loads(verified)


@router.post("/webhooks/resend")
async def resend_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    if not bridge_app.SETTINGS.resend_webhook_secret:
        raise HTTPException(
            status_code=503, detail="RESEND_WEBHOOK_SECRET is not configured"
        )

    raw_body = await request.body()
    event = verify_resend_webhook(raw_body, request)
    event_type = event.get("type")
    data = event.get("data") or {}
    email_id = str(data.get("email_id") or "").strip()
    svix_id = (
        request.headers.get("svix-id") or f"missing-svix-id:{email_id or 'unknown'}"
    )
    bridge_app.record_webhook_event(
        svix_id=svix_id,
        event_type=str(event_type or ""),
        email_id=email_id or None,
        raw_body=raw_body,
        event=event,
        headers_json=bridge_app.request_headers_json(request),
        ignored=event_type != "email.received",
    )
    if event_type != "email.received":
        bridge_app.record_processing_step(
            step="webhook",
            status=StepStatus.IGNORED,
            svix_id=svix_id,
            email_id=email_id or None,
            detail={"event_type": event_type},
        )
        return {"ok": True, "ignored": event_type}

    if not email_id:
        raise HTTPException(
            status_code=400, detail="email_id missing from webhook payload"
        )

    queued = bridge_app.record_pending_event(svix_id=svix_id, email_id=email_id)
    bridge_app.record_processing_step(
        step="webhook",
        status=StepStatus.QUEUED if queued else StepStatus.DUPLICATE,
        svix_id=svix_id,
        email_id=email_id,
    )
    if queued:
        background_tasks.add_task(bridge_app.process_event_safe, event, svix_id)
    return {"ok": True, "queued": queued, "email_id": email_id}
