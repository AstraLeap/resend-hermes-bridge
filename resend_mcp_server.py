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

from services import mailbox_store
from settings import APP_DIR
from utils.email_core import (
    clean_from_local,
    clean_header_value,
    parse_email_addresses,
)
from utils.email_display import html_to_display_text, render_draft_markdown
from utils.i18n_strings import McpMessages, NotificationTitles


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value.strip()


load_dotenv(APP_DIR / ".env")

BRIDGE_PORT = os.getenv("RESEND_BRIDGE_PORT", "8765").strip() or "8765"
BRIDGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
DRAFTS_FILE = APP_DIR / "data" / "mcp_email_drafts.json"
DRAFTS_LOCK_FILE = APP_DIR / "data" / "mcp_email_drafts.json.lock"
DRAFT_TTL_SECONDS = int(os.getenv("RESEND_MCP_DRAFT_TTL_SECONDS", "604800"))
STATE_DB_FILE = mailbox_store.STATE_DB_FILE
MCP_SERVER_NAME = "resend_email"
MCP_DISPLAY_NAME = "resend-email"
FORBIDDEN_SESSION_SOURCES = {"tool"}
FROM_DOMAIN = _require_env("RESEND_DOMAIN").lower()
OWNER_FROM_LOCAL = (
    clean_from_local(_require_env("OWNER_FROM_LOCAL")) or ""
).lower()

mcp = FastMCP(MCP_DISPLAY_NAME)

MARKDOWN_RESULT_INSTRUCTION = (
    "When replying to the user from this tool result, present the relevant "
    "information in Markdown. Prefer Markdown tables for email lists, search "
    "results, and label lists. Use concise sections or bullets for a single "
    "email detail. Preserve message_id, draft_id, labels, and external IDs "
    "exactly, formatting identifiers in backticks. Do not expose internal "
    "database paths unless the user explicitly asks for implementation details."
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_user_chat_context() -> None:
    source = str(os.getenv("HERMES_SESSION_SOURCE") or "").strip().lower()
    if source in FORBIDDEN_SESSION_SOURCES:
        raise PermissionError(
            f"{MCP_SERVER_NAME} MCP is disabled for automated tool sessions; "
            "use it only from a user chat session"
        )


def _with_markdown_result_instruction(result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in result.items()
        if key not in {"display", "assistant_response"}
    }
    return {
        "hermes_assistant_instruction": MARKDOWN_RESULT_INSTRUCTION,
        **payload,
    }


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


def _confirmation_markdown(
    draft_id: str,
    draft: dict[str, Any],
) -> str:
    return render_draft_markdown(
        draft_id,
        draft,
        title=NotificationTitles.DRAFT_CONFIRMATION,
        domain=FROM_DOMAIN,
        footer=McpMessages.DRAFT_FOOTER.format(draft_id=draft_id),
        show_attachments=False,
    )


def _sent_notification(result: dict[str, Any]) -> str:
    resend_id = result.get("resend_id") or ""
    parts = [McpMessages.SENT_NOTIFICATION]
    if resend_id:
        parts.append(McpMessages.RESEND_ID_PREFIX.format(resend_id=resend_id))
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
    to: list[str] | None,
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
    to: list[str] | None,
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


def _draft_preview_response(
    *,
    draft_id: str,
    draft: dict[str, Any],
    confirmation_markdown: str,
) -> dict[str, Any]:
    return {
        "status": "drafted",
        "draft_id": draft_id,
        "assistant_response": confirmation_markdown,
        "assistant_response_instruction": (
            "Return assistant_response to the user verbatim as your complete final "
            "message. Do not add, remove, summarize, translate, reformat, wrap in a "
            "code block, or change any Markdown."
        ),
        "metadata": _redacted_draft(draft_id, draft),
        "preview_delivered": False,
    }


async def _preview_draft(draft_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    confirmation_markdown = _confirmation_markdown(draft_id, draft)
    return _draft_preview_response(
        draft_id=draft_id,
        draft=draft,
        confirmation_markdown=confirmation_markdown,
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
    to: list[str] | None = None,
    subject: str = "",
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
    confirmed=false. It creates the draft and returns the standard preview in
    assistant_response.

    When the intended email is not clear enough to draft, do not call this tool.
    Ask the user one concise clarification question instead.

    Rules:
    - Call this tool only when the recipient (to), subject, and body (text or
      html) can be determined with reasonable confidence. It is OK to infer a
      short subject from a clear body.
    - If the recipient, body, sender identity, required attachment, factual
      content, tone/style, or other user preference is missing or ambiguous,
      ask for clarification first and do not call this tool.
    - First call with confirmed=false to create a draft. The tool returns
      assistant_response containing the complete preview and confirmation prompt.
    - When this tool returns assistant_response, your final response MUST be
      exactly assistant_response and nothing else. Do not add a prefix, suffix,
      summary, explanation, code fence, translation, or Markdown changes. Do not
      paraphrase any field. Then wait for confirmation.
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
    _require_user_chat_context()
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
        "bridge_response": result,
    }


@mcp.tool()
async def list_emails(
    mailbox: str = "all",
    label: str = "",
    status: str = "",
    limit: int = 20,
    offset: int = 0,
    include_deleted: bool = False,
) -> dict[str, Any]:
    """List local email history by mailbox, newest first.

    mailbox accepts all, inbox, sent, or trash, plus common aliases such as
    outbound/outbox/发件箱 and inbound/收件箱. Use limit and offset to page
    through results. The returned message_id and kind are the identifiers to
    pass to view_email, delete_email, and manage_email_labels. Deleted messages
    are hidden unless include_deleted is true, except trash which shows only
    deleted messages.
    """
    _require_user_chat_context()
    return _with_markdown_result_instruction(
        mailbox_store.list_mailbox(
            db_path=STATE_DB_FILE,
            mailbox=mailbox,
            label=label,
            status=status,
            limit=limit,
            offset=offset,
            include_deleted=include_deleted,
        )
    )


@mcp.tool()
async def search_emails(
    query: str = "",
    label: str = "",
    direction: str = "all",
    status: str = "",
    limit: int = 20,
    offset: int = 0,
    include_deleted: bool = False,
) -> dict[str, Any]:
    """Search local email history recorded by the bridge.

    Use list_emails when the user asks to browse a mailbox without search
    terms. direction accepts all, inbound, or outbound. The returned message_id
    and kind are the identifiers to pass to view_email, delete_email, and
    manage_email_labels. Deleted messages are hidden unless include_deleted is
    true. This tool is for user chat sessions only; automated bot email tasks
    are not allowed to use it.
    """
    _require_user_chat_context()
    return _with_markdown_result_instruction(
        mailbox_store.search_mailbox(
            db_path=STATE_DB_FILE,
            query=query,
            label=label,
            direction=direction,
            status=status,
            limit=limit,
            offset=offset,
            include_deleted=include_deleted,
        )
    )


@mcp.tool()
async def view_email(
    message_id: str,
    kind: str = "inbound",
    body_limit: int = mailbox_store.DEFAULT_BODY_LIMIT,
) -> dict[str, Any]:
    """View one recorded email.

    kind must be inbound or outbound. For inbound mail, message_id is the
    Resend email_id. For outbound mail, message_id is the numeric local
    outbound_messages id returned by search_emails.
    """
    _require_user_chat_context()
    return _with_markdown_result_instruction(
        mailbox_store.get_mailbox_email(
            db_path=STATE_DB_FILE,
            kind=kind,
            message_id=message_id,
            body_limit=body_limit,
        )
    )


@mcp.tool()
async def delete_email(
    message_id: str,
    kind: str = "inbound",
    reason: str = "",
    restore: bool = False,
) -> dict[str, Any]:
    """Soft-delete or restore a recorded email.

    Deletion hides the message from normal search results but keeps the audit
    row, attachments, and labels on disk. Pass restore=true to unhide a
    previously deleted message.
    """
    _require_user_chat_context()
    return _with_markdown_result_instruction(
        mailbox_store.delete_mailbox_email(
            db_path=STATE_DB_FILE,
            kind=kind,
            message_id=message_id,
            reason=reason,
            restore=restore,
        )
    )


@mcp.tool()
async def manage_email_labels(
    message_id: str,
    kind: str = "inbound",
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    set_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Add, remove, or replace labels on a recorded email.

    Use add_labels and remove_labels for incremental edits. Use set_labels to
    replace all labels at once. Labels are stored locally in the bridge SQLite
    database.
    """
    _require_user_chat_context()
    return _with_markdown_result_instruction(
        mailbox_store.update_mailbox_labels(
            db_path=STATE_DB_FILE,
            kind=kind,
            message_id=message_id,
            add_labels=add_labels,
            remove_labels=remove_labels,
            set_labels=set_labels,
        )
    )


@mcp.tool()
async def list_email_labels(query: str = "", limit: int = 100) -> dict[str, Any]:
    """List labels currently used in local email history."""
    _require_user_chat_context()
    return _with_markdown_result_instruction(
        mailbox_store.list_mailbox_labels(
            db_path=STATE_DB_FILE,
            query=query,
            limit=limit,
        )
    )


if __name__ == "__main__":
    mcp.run()
