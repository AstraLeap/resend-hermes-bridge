from __future__ import annotations

import re
from email.utils import parseaddr
from html import unescape
from pathlib import Path
from typing import Any

from utils.i18n_strings import EmailLabels, MailboxLabels, NotificationTitles, ProcessingMessages

HTML_BREAK_RE = re.compile(r"(?i)<\s*(br|/p|/div|/li|/tr)\b[^>]*>")
HTML_SCRIPT_STYLE_RE = re.compile(r"(?is)<\s*(script|style)\b.*?<\s*/\s*\1\s*>")
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_IMG_CID_RE = re.compile(
    r"""(?is)<\s*img\b[^>]*\bsrc\s*=\s*(["']?)cid:([^"'\s>]+)\1[^>]*>"""
)


def quote_block(text: str) -> str:
    """Render text as a markdown blockquote, preserving blank lines."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.strip():
            lines.append(f"> {raw_line}")
        else:
            lines.append(">")
    return "\n".join(lines)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def escape_markdown_table(value: Any) -> str:
    text = decode_html_entities(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def decode_html_entities(value: Any) -> str:
    return unescape(str(value or ""))


def display_address(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    name, address = parseaddr(raw)
    if name and address and "<" in raw and ">" in raw:
        return f"{name} ({address})"
    return raw


def join_addresses(value: Any) -> str:
    return ", ".join(display_address(item) for item in as_list(value) if str(item).strip())


def html_to_display_text(value: str) -> str:
    html = str(value or "").strip()
    if not html:
        return ""

    text = HTML_SCRIPT_STYLE_RE.sub("", html)
    text = HTML_BREAK_RE.sub("\n", text)
    text = HTML_TAG_RE.sub("", text)
    text = unescape(text)

    lines: list[str] = []
    previous_blank = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    return "\n".join(lines).strip()


def html_to_outbound_display_text(
    value: str,
    *,
    attachments: Any = None,
) -> str:
    cid_names = attachment_names_by_content_id(attachments)

    def replace_inline_image(match: re.Match[str]) -> str:
        content_id = unescape(match.group(2).strip())
        marker = f"{EmailLabels.INLINE_IMAGE}: cid:{content_id}"
        filename = cid_names.get(content_id)
        if filename:
            marker += f" -> {filename}"
        return f"\n[{marker}]\n"

    html = HTML_IMG_CID_RE.sub(replace_inline_image, str(value or ""))
    return html_to_display_text(html)


def attachment_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def attachment_inline_name(attachment: dict[str, Any]) -> str:
    filename = str(attachment.get("filename") or attachment.get("id") or "").strip()
    path = str(attachment.get("path") or attachment.get("local_path") or "").strip()
    if not filename and path:
        filename = Path(path).name
    return filename or "attachment"


def attachment_names_by_content_id(value: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for attachment in attachment_dicts(value):
        content_id = str(attachment.get("content_id") or "").strip()
        if content_id and content_id not in names:
            names[content_id] = attachment_inline_name(attachment)
    return names


def format_sender(payload: dict[str, Any], *, domain: str) -> str:
    explicit = str(payload.get("from") or payload.get("from_email") or "").strip()
    if explicit:
        return display_address(explicit)
    local = str(payload.get("from_local") or "bot").strip()
    address = f"{local}@{domain}"
    from_name = str(payload.get("from_name") or "").strip()
    return f"{from_name} ({address})" if from_name else address


def email_display_rows(
    payload: dict[str, Any],
    *,
    domain: str,
    draft_id: str | None = None,
    email_id: str | None = None,
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if draft_id:
        rows.append((EmailLabels.DRAFT_ID, draft_id))
    if email_id:
        rows.append((EmailLabels.EMAIL_ID, email_id))
    rows.extend(
        [
            (EmailLabels.FROM, format_sender(payload, domain=domain)),
            (EmailLabels.TO, join_addresses(payload.get("to"))),
        ]
    )
    for key, label in (
        ("cc", EmailLabels.CC),
        ("bcc", EmailLabels.BCC),
        ("reply_to", EmailLabels.REPLY_TO),
    ):
        joined = join_addresses(payload.get(key))
        if joined:
            rows.append((label, joined))
    rows.append((EmailLabels.SUBJECT, str(payload.get("subject") or "")))
    return rows


def email_body_block(
    payload: dict[str, Any],
    *,
    body_limit: int | None = None,
    prefer_html: bool = False,
) -> tuple[str, str]:
    text = str(payload.get("text") or "").strip()
    html = str(payload.get("html") or "").strip()
    if prefer_html and html:
        label = EmailLabels.HTML_BODY
        body = (
            html_to_outbound_display_text(html, attachments=payload.get("attachments"))
            or EmailLabels.EMPTY_BODY
        )
    elif text:
        label = EmailLabels.BODY
        body = text
    elif html:
        label = EmailLabels.HTML_BODY
        body = html_to_display_text(html) or EmailLabels.EMPTY_BODY
    else:
        label = EmailLabels.BODY
        body = EmailLabels.EMPTY_BODY
    if body_limit is not None and len(body) > body_limit:
        body = body[:body_limit] + ProcessingMessages.TRUNCATED
    return label, body


def render_markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(escape_markdown_table(header) for header in headers) + " |",
        "| " + " | ".join("---" for _header in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(escape_markdown_table(value) for value in row) + " |")
    return lines


def render_attachments_markdown(
    attachments: list[dict[str, Any]] | None,
) -> list[str]:
    if not attachments:
        return []
    lines = [
        "",
        f"**{EmailLabels.ATTACHMENTS}**",
        "",
    ]
    rows: list[list[str]] = []
    for attachment in attachments[:12]:
        filename = attachment_display_name(attachment)
        size = attachment_display_size(attachment)
        rows.append([filename, size])
    if len(attachments) > 12:
        rows.append(
            [
                "...",
                EmailLabels.MORE_ATTACHMENTS.format(count=len(attachments) - 12),
            ]
        )
    lines.extend(render_markdown_table([EmailLabels.FILE, EmailLabels.SIZE], rows))
    return lines


def attachment_display_name(attachment: dict[str, Any]) -> str:
    filename = str(attachment.get("filename") or attachment.get("id") or "").strip()
    path = str(attachment.get("path") or attachment.get("local_path") or "").strip()
    content_id = str(attachment.get("content_id") or "").strip()
    if not filename and path:
        filename = Path(path).name
    inline_suffix = ""
    if content_id:
        stem = Path(filename).stem if filename else ""
        if stem == content_id or filename == content_id:
            inline_suffix = f" [{EmailLabels.INLINE_IMAGE}]"
        else:
            inline_suffix = f" [{EmailLabels.INLINE_IMAGE}: cid:{content_id}]"
    return f"{filename or 'attachment'}{inline_suffix}"


def _format_size(bytes_value: int) -> str:
    if bytes_value < 1024:
        return f"{bytes_value} B"
    kb = bytes_value / 1024
    if kb < 1024:
        if kb >= 10:
            return f"{int(round(kb))} KB"
        return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb >= 10:
        return f"{int(round(mb))} MB"
    return f"{mb:.1f} MB"


def attachment_display_size(attachment: dict[str, Any]) -> str:
    size = attachment.get("size")
    if size not in (None, ""):
        try:
            return _format_size(int(size))
        except (ValueError, TypeError):
            pass
    path = str(attachment.get("path") or attachment.get("local_path") or "").strip()
    if path:
        try:
            return _format_size(Path(path).expanduser().stat().st_size)
        except OSError:
            pass
    content = str(attachment.get("content") or "").strip()
    if content:
        compact = re.sub(r"\s+", "", content)
        padding = len(compact) - len(compact.rstrip("="))
        decoded_size = max((len(compact) * 3) // 4 - padding, 0)
        return _format_size(decoded_size)
    return "unknown size"


def render_email_markdown(
    payload: dict[str, Any],
    *,
    title: str | None,
    domain: str,
    draft_id: str | None = None,
    email_id: str | None = None,
    footer: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    body_limit: int | None = None,
    notice_limit: int | None = None,
    show_attachments: bool = True,
    prefer_html_body: bool = False,
) -> str:
    lines: list[str] = []
    if title:
        lines.extend([title, ""])
    rows = [
        [label, value]
        for label, value in email_display_rows(
            payload,
            domain=domain,
            draft_id=draft_id,
            email_id=email_id,
        )
    ]
    lines.extend(render_markdown_table([EmailLabels.FIELD, EmailLabels.CONTENT], rows))

    body_label, body = email_body_block(
        payload,
        body_limit=body_limit,
        prefer_html=prefer_html_body,
    )
    body = decode_html_entities(body).replace("```", "'''")
    lines.extend(
        [
            "",
            f"**{body_label}**",
            "",
            quote_block(body),
        ]
    )
    if show_attachments:
        display_attachments = attachments
        if display_attachments is None:
            display_attachments = payload.get("attachments")
        lines.extend(render_attachments_markdown(display_attachments))
    if footer:
        lines.extend(["", decode_html_entities(footer)])
    notice = "\n".join(lines)
    return truncate_notice(notice, notice_limit) if notice_limit is not None else notice


def render_draft_markdown(
    draft_id: str,
    draft: dict[str, Any],
    *,
    title: str,
    domain: str,
    footer: str | None = None,
    show_attachments: bool = True,
) -> str:
    return render_email_markdown(
        draft["payload"],
        title=title,
        domain=domain,
        draft_id=draft_id,
        footer=footer,
        show_attachments=show_attachments,
        prefer_html_body=True,
    )


def inbound_email_payload(email: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": email.get("from"),
        "to": email.get("to"),
        "cc": email.get("cc"),
        "bcc": email.get("bcc"),
        "subject": email.get("subject") or MailboxLabels.NO_SUBJECT,
        "text": email.get("text") or "",
        "html": email.get("html") or "",
    }


def render_inbound_email_notice(
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
    *,
    title: str,
    domain: str,
    footer: str | None = None,
    body_limit: int = 1800,
    notice_limit: int = 3800,
    show_attachments: bool = True,
) -> str:
    inbound_id = str(email.get("id") or "").strip() or None
    return render_email_markdown(
        inbound_email_payload(email),
        title=title,
        domain=domain,
        email_id=inbound_id,
        footer=footer,
        attachments=attachments,
        body_limit=body_limit,
        notice_limit=notice_limit,
        show_attachments=show_attachments,
    )


def render_processing_result_notice(
    summary: str,
    decision: dict[str, Any] | None = None,
    *,
    domain: str,
    ai_name: str = "Hermes",
    reply_payload: dict[str, Any] | None = None,
    reply_id: str | None = None,
    notice_limit: int = 3800,
    show_attachments: bool = True,
) -> str:
    if reply_payload:
        footer = ProcessingMessages.REPLY_FOOTER.format(reply_id=reply_id) if reply_id else None
        notice = render_email_markdown(
            reply_payload,
            title=NotificationTitles.AUTO_REPLY_SENT.format(ai_name=ai_name),
            domain=domain,
            footer=footer,
            notice_limit=notice_limit,
            show_attachments=show_attachments,
            prefer_html_body=True,
        )
        return notice

    return truncate_notice(notice_footer(summary, decision), notice_limit)


def notice_footer(
    summary: str,
    decision: dict[str, Any] | None = None,
    *,
    reply_id: str | None = None,
) -> str:
    sections = [
        ProcessingMessages.RESULT_SECTION,
        "",
        summary,
    ]
    if reply_id:
        sections.extend(["", ProcessingMessages.REPLY_FOOTER.format(reply_id=reply_id)])
    return "\n".join(sections)


def truncate_notice(message: str, limit: int = 3800) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 16].rstrip() + ProcessingMessages.TRUNCATED
