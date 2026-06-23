from __future__ import annotations

from typing import Any

from utils.email_display import inbound_email_payload, render_email_markdown


def render_inbound_email_notice(
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
    *,
    title: str,
    domain: str,
    footer: str | None = None,
    body_limit: int = 1800,
    notice_limit: int = 3800,
) -> str:
    inbound_id = str(email.get("id") or "").strip() or None
    notice = render_email_markdown(
        inbound_email_payload(email),
        title=title,
        domain=domain,
        email_id=inbound_id,
        footer=footer,
        attachments=attachments,
        body_limit=body_limit,
    )
    return truncate_notice(notice, notice_limit)


def render_processing_result_notice(
    summary: str,
    decision: dict[str, Any] | None = None,
    *,
    domain: str,
    reply_payload: dict[str, Any] | None = None,
    reply_id: str | None = None,
    notice_limit: int = 3800,
) -> str:
    if reply_payload:
        notice = render_email_markdown(
            reply_payload,
            title="Hermes 已自动回复：",
            domain=domain,
            email_id=reply_id,
        )
        return truncate_notice(notice, notice_limit)

    return truncate_notice(notice_footer(summary, decision), notice_limit)


def notice_footer(
    summary: str,
    decision: dict[str, Any] | None = None,
    *,
    reply_id: str | None = None,
) -> str:
    sections = [
        "**处理结果**",
        "",
        summary,
    ]
    if reply_id:
        sections.extend(["", f"Resend ID: `{reply_id}`"])
    return "\n".join(sections)


def truncate_notice(message: str, limit: int = 3800) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 16].rstrip() + "\n...[truncated]"
