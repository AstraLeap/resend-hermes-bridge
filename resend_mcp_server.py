#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from settings import APP_DIR
from utils.email_core import (
    clean_from_local,
    clean_header_value,
    parse_email_addresses,
)
from utils.email_display import html_to_display_text, render_draft_markdown


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value.strip()


load_dotenv(APP_DIR / ".env")

BRIDGE_URL = os.getenv("RESEND_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
DRAFTS_FILE = APP_DIR / "data" / "mcp_email_drafts.json"
DRAFTS_LOCK_FILE = APP_DIR / "data" / "mcp_email_drafts.json.lock"
DRAFT_TTL_SECONDS = int(os.getenv("RESEND_MCP_DRAFT_TTL_SECONDS", "604800"))
FROM_DOMAIN = _require_env("RESEND_DOMAIN").lower()
OWNER_FROM_LOCAL = (
    clean_from_local(_require_env("OWNER_FROM_LOCAL")) or ""
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
        show_attachments=False,
    )


def _sent_notification(result: dict[str, Any]) -> str:
    resend_id = result.get("resend_id") or ""
    parts = ["邮件已通过 Resend 发送。"]
    if resend_id:
        parts.append(f"Resend ID: `{resend_id}`")
    return "\n".join(parts)


def _normalize_manual_from_local(value: Any) -> str:
    return (clean_from_local(value) or OWNER_FROM_LOCAL).lower()


_PREVIEW_TEMPLATE_PATTERNS = (
    re.compile(r"请确认是否发送以下邮件"),
    re.compile(r"确认后我会发送\s*Draft ID", re.IGNORECASE),
    re.compile(r"邮件草稿已创建"),
    re.compile(r"^\s*\|\s*(字段|项目|Field)\s*\|\s*(内容|Content)\s*\|", re.MULTILINE),
    re.compile(r"^\s*\|\s*(Draft ID|From|To|Subject)\s*\|", re.IGNORECASE | re.MULTILINE),
)


def _normalize_body_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


def _looks_like_chat_preview(value: str) -> bool:
    text = _normalize_body_text(value)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PREVIEW_TEMPLATE_PATTERNS)


def _reject_chat_preview_body(field: str, value: str) -> None:
    if _looks_like_chat_preview(value):
        raise ValueError(
            f"{field} looks like a chat preview or confirmation template; "
            "pass only the actual email body to text/html"
        )


def _normalize_html_body(value: Any) -> str:
    html = str(value or "").strip()
    if not html:
        return ""
    _reject_chat_preview_body("html", html_to_display_text(html) or html)
    return html


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

    body_text = _normalize_body_text(text)
    body_html = _normalize_html_body(html)
    if not body_text and not body_html:
        raise ValueError("text or html body is required")
    if body_text:
        _reject_chat_preview_body("text", body_text)
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


async def _show_draft_via_bridge(
    payload: dict[str, Any],
    *,
    draft_id: str,
    title: str = "请确认是否发送以下邮件：",
    footer: str = "",
) -> None:
    body = {
        "payload": payload,
        "draft_id": draft_id,
        "title": title,
    }
    if footer:
        body["footer"] = footer
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{BRIDGE_URL}/show-draft",
            headers={"Content-Type": "application/json"},
            json=body,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Bridge show-draft failed ({response.status_code}): {response.text}")


def _load_draft(draft_id: str) -> dict[str, Any] | None:
    draft_id = str(draft_id or "").strip()
    if not draft_id:
        return None
    with _locked_drafts() as drafts:
        stored = drafts.get(draft_id)
        return dict(stored) if isinstance(stored, dict) else None


def _new_draft_record(payload: dict[str, Any], *, revision_of: str = "") -> dict[str, Any]:
    draft: dict[str, Any] = {
        "created_at": _now_iso(),
        "payload": payload,
        "sent": False,
        "approval_token": uuid.uuid4().hex,
    }
    if revision_of:
        draft["revision_of"] = revision_of
    return draft


def _create_draft(payload: dict[str, Any], *, revision_of: str = "") -> tuple[str, dict[str, Any]]:
    revision_of = str(revision_of or "").strip()
    draft_id = uuid.uuid4().hex[:12]
    draft = _new_draft_record(payload, revision_of=revision_of)
    with _locked_drafts() as drafts:
        if revision_of:
            previous = drafts.get(revision_of)
            if not isinstance(previous, dict):
                raise ValueError(f"unknown or expired revision_of draft_id: {revision_of}")
            revisions = previous.setdefault("revisions", [])
            if isinstance(revisions, list):
                revisions.append(draft_id)
            drafts[revision_of] = previous
        drafts[draft_id] = draft
    return draft_id, draft


def _payload_inputs_present(
    *,
    to: list[str],
    subject: str,
    text: str,
    html: str,
    from_local: str,
    from_name: str,
    cc: list[str] | None,
    bcc: list[str] | None,
    reply_to: list[str] | None,
    attachments: list[dict[str, Any]] | None,
    attachment_paths: list[str] | None,
) -> bool:
    return any(
        [
            bool(to),
            bool(str(subject or "").strip()),
            bool(str(text or "").strip()),
            bool(str(html or "").strip()),
            bool(str(from_local or "").strip()),
            bool(str(from_name or "").strip()),
            bool(cc),
            bool(bcc),
            bool(reply_to),
            bool(attachments),
            bool(attachment_paths),
        ]
    )


def _draft_success_response(draft_id: str) -> dict[str, Any]:
    return {
        "status": "drafted",
        "draft_id": draft_id,
        "preview_delivered": True,
    }


def _draft_fallback_response(
    *,
    draft_id: str,
    draft: dict[str, Any],
    confirmation_markdown: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "status": "drafted",
        "draft_id": draft_id,
        "assistant_response": f"{confirmation_markdown}\n\n（无法通过桥接发送富文本预览：{error}）",
        "display": confirmation_markdown,
        "metadata": _redacted_draft(draft_id, draft),
        "preview_delivered": False,
    }


async def _preview_draft(draft_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    payload = dict(draft["payload"])
    confirmation_markdown = _confirmation_markdown(draft_id, draft)
    footer = f"确认后我会发送 Draft ID `{draft_id}`。"
    try:
        await _show_draft_via_bridge(
            payload,
            draft_id=draft_id,
            title="请确认是否发送以下邮件：",
            footer=footer,
        )
        return _draft_success_response(draft_id)
    except Exception as exc:
        return _draft_fallback_response(
            draft_id=draft_id,
            draft=draft,
            confirmation_markdown=confirmation_markdown,
            error=exc,
        )


def _load_draft_for_sending(draft_id: str) -> tuple[dict[str, Any], str]:
    with _locked_drafts() as drafts:
        stored = drafts.get(draft_id)
        if not isinstance(stored, dict):
            raise ValueError(f"unknown or expired draft_id: {draft_id}")
        if stored.get("sent"):
            raise ValueError(f"draft_id already sent: {draft_id}")
        approval_token = str(stored.get("approval_token") or "").strip()
        if not approval_token:
            raise ValueError(f"draft_id is missing approval metadata; recreate the draft: {draft_id}")
        return dict(stored), approval_token


def _mark_draft_send_failed(draft_id: str, error: Exception) -> None:
    with _locked_drafts() as drafts:
        stored = drafts.get(draft_id)
        if isinstance(stored, dict):
            stored["sending"] = False
            stored["last_error"] = str(error)[:1000]
            drafts[draft_id] = stored


def _mark_draft_sent(draft_id: str, result: dict[str, Any]) -> None:
    with _locked_drafts() as drafts:
        stored = drafts.get(draft_id)
        if isinstance(stored, dict):
            stored["sending"] = False
            stored["sent"] = True
            stored["sent_at"] = _now_iso()
            stored["bridge_response"] = result
            drafts[draft_id] = stored


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
    revision_of: str = "",
    confirmed: bool = False,
) -> dict[str, Any]:
    """Create an email draft preview, or send a previously user-confirmed draft.

    When the intended email is clear enough to draft, call this tool with
    confirmed=false. It creates the draft and displays the standard preview to
    the user. Do not write, summarize, or display your own preview in chat.

    When the intended email is not clear enough to draft, do not call this tool.
    Ask the user one concise clarification question instead.

    Rules:
    - Call this tool only when the recipient (to), subject, and body (text or
      html) can be determined with reasonable confidence. It is OK to infer a
      short subject from a clear body.
    - If the recipient, body, sender identity, required attachment, factual
      content, tone/style, or other user preference is missing or ambiguous,
      ask for clarification first and do not call this tool.
    - First call with confirmed=false to create a draft. The preview is shown
      automatically; do not output any extra text in the chat.
    - If this tool returns preview_delivered=true, end your turn silently; the
      user already has the standard preview and confirmation prompt.
    - If this tool returns assistant_response, show exactly assistant_response
      to the user and wait for confirmation.
    - Only after the user confirms, call again with confirmed=true and the
      returned draft_id. The payload must match the draft exactly.
    - If the user asks to modify a previous draft, create a new draft with the
      revised payload. Omit draft_id, and pass the old draft id as revision_of
      when known. Do not put a preview table or confirmation text in text/html.
      Ensure the revised subject, body, attachments, and recipients are
      internally consistent; do not preserve old subject wording that conflicts
      with the revised body.
    - confirmed=false with draft_id only re-shows that existing draft; it does
      not update the draft content.
    - Manual sends without a prior draft_id are rejected.

    Use from_local to choose any valid local part under the configured domain.
    Omit from_local to use the configured owner sender.
    To attach files, pass attachment_paths for local files or attachments as
    objects with path, or filename plus base64 content.
    """
    draft_id = str(draft_id or "").strip()
    revision_of = str(revision_of or "").strip()

    if not confirmed:
        payload_input_present = _payload_inputs_present(
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
        if draft_id:
            if revision_of:
                raise ValueError("use revision_of without draft_id when creating a revised draft")
            draft = _load_draft(draft_id)
            if draft is None:
                raise ValueError(f"unknown or expired draft_id: {draft_id}")
            if payload_input_present:
                raise ValueError(
                    "confirmed=false with draft_id re-shows the existing draft; "
                    "to revise it, omit draft_id and pass the old id as revision_of"
                )
            return await _preview_draft(draft_id, draft)

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
        new_draft_id, draft = _create_draft(payload, revision_of=revision_of)
        return await _preview_draft(new_draft_id, draft)

    if not draft_id:
        raise ValueError(
            "confirmed=true requires a draft_id from a prior draft response; "
            "first create a draft and show it to the user for confirmation"
        )
    if revision_of:
        raise ValueError("confirmed=true uses draft_id only; do not pass revision_of")

    draft, approval_token = _load_draft_for_sending(draft_id)
    payload = dict(draft["payload"])

    payload["confirmed"] = True
    payload["draft_id"] = draft_id
    payload["approval_token"] = approval_token
    try:
        result = await _send_via_bridge(payload)
    except Exception as exc:
        _mark_draft_send_failed(draft_id, exc)
        raise
    _mark_draft_sent(draft_id, result)
    return {
        "status": "sent",
        "assistant_response": _sent_notification(result),
        "display": _sent_notification(result),
        "bridge_response": result,
    }


if __name__ == "__main__":
    mcp.run()
