#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from utils.email_core import (
    clean_from_local,
    clean_header_value,
    parse_email_addresses,
)
from utils.email_display import render_draft_markdown


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value.strip()


APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")

BRIDGE_URL = os.getenv("RESEND_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
DRAFTS_FILE = APP_DIR / "mcp_email_drafts.json"
DRAFTS_LOCK_FILE = APP_DIR / "mcp_email_drafts.json.lock"
DRAFT_TTL_SECONDS = int(os.getenv("RESEND_MCP_DRAFT_TTL_SECONDS", "604800"))
FROM_DOMAIN = _require_env("RESEND_DOMAIN").lower()
BOT_FROM_LOCAL = (
    clean_from_local(_require_env("BOT_FROM_LOCAL")) or ""
).lower()
OWNER_FROM_LOCAL = (
    clean_from_local(_require_env("OWNER_FROM_LOCAL")) or ""
).lower()
DEFAULT_FROM_LOCAL = (
    clean_from_local(os.getenv("RESEND_DEFAULT_FROM_LOCAL", OWNER_FROM_LOCAL))
    or OWNER_FROM_LOCAL
).lower()

mcp = FastMCP("resend-email")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _load_drafts_unlocked() -> dict[str, Any]:
    if not DRAFTS_FILE.exists():
        return {}
    try:
        data = json.loads(DRAFTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_drafts_unlocked(drafts: dict[str, Any]) -> None:
    DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DRAFTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(drafts, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(DRAFTS_FILE)
    DRAFTS_FILE.chmod(0o600)


def _prune_expired_drafts(drafts: dict[str, Any]) -> None:
    if DRAFT_TTL_SECONDS <= 0:
        return
    now = datetime.now(UTC)
    expired: list[str] = []
    for draft_id, draft in drafts.items():
        if not isinstance(draft, dict):
            expired.append(draft_id)
            continue
        created_at = _parse_iso(draft.get("created_at"))
        sent_at = _parse_iso(draft.get("sent_at"))
        timestamp = sent_at or created_at
        if timestamp is None:
            expired.append(draft_id)
            continue
        if (now - timestamp).total_seconds() > DRAFT_TTL_SECONDS:
            expired.append(draft_id)
    for draft_id in expired:
        drafts.pop(draft_id, None)


@contextmanager
def _locked_drafts():
    DRAFTS_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DRAFTS_LOCK_FILE.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            drafts = _load_drafts_unlocked()
            _prune_expired_drafts(drafts)
            yield drafts
            _save_drafts_unlocked(drafts)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _redacted_draft(draft_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    payload = draft["payload"]
    return {
        "draft_id": draft_id,
        "from": payload.get("from_local") or f"default ({FROM_DOMAIN})",
        "to": payload.get("to", []),
        "cc": payload.get("cc", []),
        "bcc_count": len(payload.get("bcc", [])),
        "subject": payload.get("subject", ""),
        "has_text": bool(payload.get("text")),
        "has_html": bool(payload.get("html")),
        "attachment_count": len(payload.get("attachments") or []),
        "created_at": draft.get("created_at"),
    }


def _confirmation_markdown(draft_id: str, draft: dict[str, Any]) -> str:
    return render_draft_markdown(
        draft_id,
        draft,
        title="请确认是否发送以下邮件：",
        domain=FROM_DOMAIN,
        footer=f"确认后我会发送 Draft ID `{draft_id}`。",
    )


def _draft_payload_for_display(payload: dict[str, Any]) -> dict[str, Any]:
    display_payload = dict(payload)
    attachments = []
    for item in payload.get("attachments") or []:
        if isinstance(item, dict):
            attachment = {
                key: value
                for key, value in item.items()
                if key in {"filename", "path", "local_path", "content_type", "content_id", "size", "id"}
                and value not in (None, "", [])
            }
            if item.get("content"):
                attachment["content_redacted"] = True
            attachments.append(attachment)
    if attachments:
        display_payload["attachments"] = attachments
    return display_payload


def _normalize_manual_from_local(value: Any) -> str:
    return (clean_from_local(value) or DEFAULT_FROM_LOCAL).lower()


def _format_outbound_payload(
    *,
    to: list[str],
    subject: str,
    text: str = "",
    html: str = "",
    from_local: str = "",
    from_name: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    attachment_paths: list[str] | None = None,
) -> dict[str, Any]:
    clean_to = parse_email_addresses(to, "to", required=True)
    clean_local = _normalize_manual_from_local(from_local)
    payload: dict[str, Any] = {
        "from_local": clean_local,
        "to": clean_to,
        "subject": clean_header_value(subject, "subject", required=True),
    }
    clean_from_name = clean_header_value(from_name, "from_name", limit=120)
    if clean_from_name:
        payload["from_name"] = clean_from_name
    clean_cc = parse_email_addresses(cc, "cc")
    clean_bcc = parse_email_addresses(bcc, "bcc")
    clean_reply_to = parse_email_addresses(reply_to, "reply_to")
    if clean_cc:
        payload["cc"] = clean_cc
    if clean_bcc:
        payload["bcc"] = clean_bcc
    if clean_reply_to:
        payload["reply_to"] = clean_reply_to

    body_text = str(text or "").strip()
    body_html = str(html or "").strip()
    if not body_text and not body_html:
        raise ValueError("text or html body is required")
    if body_text:
        payload["text"] = body_text
    if body_html:
        payload["html"] = body_html
    attachment_specs: list[dict[str, Any]] = []
    for path in attachment_paths or []:
        clean_path = str(path or "").strip()
        if clean_path:
            attachment_specs.append({"path": clean_path})
    for attachment in attachments or []:
        if not isinstance(attachment, dict):
            raise ValueError("attachments must be objects with path or base64 content")
        attachment_specs.append(dict(attachment))
    if attachment_specs:
        payload["attachments"] = attachment_specs
    return payload


async def _send_via_bridge(payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            f"{BRIDGE_URL}/send",
            headers={"Content-Type": "application/json"},
            json=payload,
        )
    try:
        response_body = response.json()
    except ValueError:
        response_body = {"text": response.text}
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response_body.get("detail") if isinstance(response_body, dict) else response_body
        raise RuntimeError(f"Bridge send failed ({response.status_code}): {detail}") from exc
    return response_body if isinstance(response_body, dict) else {"data": response_body}


@mcp.tool()
async def send_email(
    to: list[str],
    subject: str,
    text: str = "",
    html: str = "",
    from_local: str = "",
    from_name: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    attachment_paths: list[str] | None = None,
    draft_id: str = "",
    confirmed: bool = False,
    auto_reply_email_id: str = "",
) -> dict[str, Any]:
    """Create an email draft preview, or send a previously user-confirmed draft.

    First call this without confirmed=true so the user can review the draft.
    Only after the user confirms that exact draft, call again with confirmed=true
    and the returned draft_id. Manual sends without a prior draft_id are rejected.
    Use from_local to choose any valid local part under the configured domain.
    Omit from_local to use the configured owner sender; the bot sender is allowed.
    To attach files, pass attachment_paths for local files or attachments as
    objects with path, or filename plus base64 content.
    """
    if auto_reply_email_id.strip():
        from_local = BOT_FROM_LOCAL

    draft_id = str(draft_id or "").strip()
    draft = None
    approval_token = ""
    if draft_id:
        with _locked_drafts() as drafts:
            stored = drafts.get(draft_id)
            draft = dict(stored) if isinstance(stored, dict) else None
            if draft is None and confirmed:
                raise ValueError(f"unknown or expired draft_id: {draft_id}")

    if draft is not None:
        payload = dict(draft["payload"])
    else:
        payload = _format_outbound_payload(
            to=to,
            subject=subject,
            text=text,
            html=html,
            from_local=from_local,
            from_name=from_name,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
            attachment_paths=attachment_paths,
        )

    if auto_reply_email_id.strip():
        payload["confirmed"] = True
        payload["auto_reply_email_id"] = auto_reply_email_id.strip()
        result = await _send_via_bridge(payload)
        return {
            "status": "sent",
            "assistant_response": render_draft_markdown(
                result.get("outbound_id") or uuid.uuid4().hex[:12],
                {"payload": _draft_payload_for_display(payload), "created_at": _now_iso(), "sent": True},
                title="已发送以下邮件：",
                domain=FROM_DOMAIN,
                footer=f"Resend ID: `{result.get('resend_id') or ''}`",
            ),
            "display": render_draft_markdown(
                result.get("outbound_id") or uuid.uuid4().hex[:12],
                {"payload": _draft_payload_for_display(payload), "created_at": _now_iso(), "sent": True},
                title="已发送以下邮件：",
                domain=FROM_DOMAIN,
                footer=f"Resend ID: `{result.get('resend_id') or ''}`",
            ),
            "bridge_response": result,
        }

    if not confirmed:
        if draft is None:
            draft_id = uuid.uuid4().hex[:12]
            draft = {
                "created_at": _now_iso(),
                "payload": payload,
                "sent": False,
                "approval_token": uuid.uuid4().hex,
            }
            with _locked_drafts() as drafts:
                drafts[draft_id] = draft
        assert draft is not None
        confirmation_markdown = _confirmation_markdown(draft_id, draft)
        return {
            "status": "drafted",
            "draft_id": draft_id,
            "assistant_response": confirmation_markdown,
            "display": confirmation_markdown,
            "confirmation_markdown": confirmation_markdown,
            "metadata": _redacted_draft(draft_id, draft),
            "next_step": "Reply to the user with assistant_response verbatim. Do not rewrite the table, body, labels, wording, separators, or confirmation prompt. Call send_email again with confirmed=true after the user confirms sending this draft.",
        }

    if not draft_id:
        raise ValueError(
            "confirmed=true requires a draft_id from a prior draft response; "
            "first create a draft and show it to the user for confirmation"
        )

    if draft_id:
        with _locked_drafts() as drafts:
            stored = drafts.get(draft_id)
            if not isinstance(stored, dict):
                raise ValueError(f"unknown or expired draft_id: {draft_id}")
            if stored.get("sent"):
                raise ValueError(f"draft_id already sent: {draft_id}")
            if stored.get("sending"):
                raise ValueError(f"draft_id is already being sent: {draft_id}")
            approval_token = str(stored.get("approval_token") or "").strip()
            if not approval_token:
                raise ValueError(f"draft_id is missing approval metadata; recreate the draft: {draft_id}")
            stored["sending"] = True
            stored["sending_at"] = _now_iso()
            stored.pop("last_error", None)
            drafts[draft_id] = stored

    payload["confirmed"] = True
    payload["draft_id"] = draft_id
    payload["approval_token"] = approval_token
    try:
        result = await _send_via_bridge(payload)
    except Exception as exc:
        if draft_id:
            with _locked_drafts() as drafts:
                stored = drafts.get(draft_id)
                if isinstance(stored, dict):
                    stored["sending"] = False
                    stored["last_error"] = str(exc)[:1000]
                    drafts[draft_id] = stored
        raise
    if draft_id:
        with _locked_drafts() as drafts:
            stored = drafts.get(draft_id)
            if isinstance(stored, dict):
                stored["sending"] = False
                stored["sent"] = True
                stored["sent_at"] = _now_iso()
                stored["bridge_response"] = result
                drafts[draft_id] = stored
    return {
        "status": "sent",
        "assistant_response": render_draft_markdown(
            result.get("outbound_id") or uuid.uuid4().hex[:12],
            {"payload": _draft_payload_for_display(payload), "created_at": _now_iso(), "sent": True},
            title="已发送以下邮件：",
            domain=FROM_DOMAIN,
            footer=f"Resend ID: `{result.get('resend_id') or ''}`",
        ),
        "display": render_draft_markdown(
            result.get("outbound_id") or uuid.uuid4().hex[:12],
            {"payload": _draft_payload_for_display(payload), "created_at": _now_iso(), "sent": True},
            title="已发送以下邮件：",
            domain=FROM_DOMAIN,
            footer=f"Resend ID: `{result.get('resend_id') or ''}`",
        ),
        "bridge_response": result,
    }


if __name__ == "__main__":
    mcp.run()
