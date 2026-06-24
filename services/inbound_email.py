from __future__ import annotations

from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx

import services.resend_client as resend_client
from services.hermes_client import run_hermes_email_task
from services.resend_outbound import (
    build_resend_reply_payload,
    reply_text_from_decision,
    send_resend_reply,
)
from utils.email_display import render_inbound_email_notice, render_processing_result_notice


class _BridgeAppProxy:
    def __getattr__(self, name: str) -> Any:
        import app as bridge_app

        return getattr(bridge_app, name)


bridge_app = _BridgeAppProxy()


def is_to_inbound_address(email: dict[str, Any], event_data: dict[str, Any]) -> bool:
    recipients: list[str] = []
    for source in (email, event_data):
        for key in ("to", "cc", "bcc"):
            value = source.get(key)
            if isinstance(value, list):
                recipients.extend(str(item) for item in value)
            elif value:
                recipients.append(str(value))
    parsed = {parseaddr(item)[1].lower() or item.lower() for item in recipients}
    return bridge_app.SETTINGS.inbound_address in parsed


def is_relevant_attachment(attachment: dict[str, Any]) -> bool:
    return True


def read_text_snippet(
    path: Path, attachment: dict[str, Any], limit: int = 6000
) -> str | None:
    content_type = str(attachment.get("content_type") or "").lower()
    if path.suffix.lower() not in bridge_app.TEXT_EXTENSIONS and not (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/xml"}
    ):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def record_fetched_attachment_metadata(
    email_id: str, attachments: list[dict[str, Any]]
) -> None:
    for attachment in attachments:
        item = {
            "id": attachment.get("id"),
            "filename": attachment.get("filename")
            or attachment.get("id")
            or "attachment",
            "content_type": attachment.get("content_type"),
            "size": int(attachment.get("size") or 0),
            "relevant": True,
        }
        bridge_app.record_attachment_history(
            email_id=email_id, raw_attachment=attachment, item=item
        )


def notification_attachment_paths(downloaded: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for item in downloaded:
        path = str(item.get("local_path") or "").strip()
        if path:
            paths.append(path)
    return paths


async def download_attachments_for_notification(
    email_id: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not attachments:
        return []
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            return await download_relevant_attachments(client, email_id, attachments)
    except Exception as exc:
        bridge_app.LOGGER.warning(
            "could not download attachments for email %s notification: %s",
            email_id,
            exc,
        )
        return []


def _validate_agent_attachment_paths(paths: list[str]) -> list[str]:
    allowed_roots = bridge_app.agent_attachment_roots()
    valid: list[str] = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = bridge_app.APP_DIR / path
        path = path.resolve()
        if not path.is_file():
            bridge_app.LOGGER.warning("owner_report_attachment does not exist: %s", raw)
            continue
        if not any(
            bridge_app._path_is_relative_to(path, root) for root in allowed_roots
        ):
            bridge_app.LOGGER.warning(
                "owner_report_attachment outside allowed directories: %s", raw
            )
            continue
        valid.append(str(path))
    return valid


async def download_relevant_attachments(
    client: httpx.AsyncClient,
    email_id: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_dir = bridge_app.SETTINGS.attachment_dir / bridge_app.safe_filename(email_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    results: list[dict[str, Any]] = []

    for attachment in attachments:
        filename = str(
            attachment.get("filename") or attachment.get("id") or "attachment"
        )
        size = int(attachment.get("size") or 0)
        item: dict[str, Any] = {
            "id": attachment.get("id"),
            "filename": filename,
            "content_type": attachment.get("content_type"),
            "size": size,
            "relevant": True,
        }
        results.append(item)

        download_url = attachment.get("download_url")
        if not download_url:
            bridge_app.record_attachment_history(
                email_id=email_id, raw_attachment=attachment, item=item
            )
            continue
        if size and size > bridge_app.SETTINGS.max_attachment_bytes:
            item["skipped"] = f"larger than {bridge_app.SETTINGS.max_attachment_bytes} bytes"
            bridge_app.record_attachment_history(
                email_id=email_id, raw_attachment=attachment, item=item
            )
            continue

        path = bridge_app.unique_path(target_dir / bridge_app.safe_filename(filename))
        tmp_path = path.with_name(f"{path.name}.part")
        try:
            total = 0
            async with client.stream(
                "GET",
                str(download_url),
                headers={"User-Agent": resend_client.USER_AGENT},
                timeout=90,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > bridge_app.SETTINGS.max_attachment_bytes:
                        raise ValueError(
                            f"larger than {bridge_app.SETTINGS.max_attachment_bytes} bytes"
                        )
                with tmp_path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > bridge_app.SETTINGS.max_attachment_bytes:
                            raise ValueError(
                                f"larger than {bridge_app.SETTINGS.max_attachment_bytes} bytes"
                            )
                        output.write(chunk)
            tmp_path.replace(path)
            path.chmod(0o600)
            item["size"] = total

            snippet = read_text_snippet(path, attachment)
            if snippet:
                item["text_snippet"] = snippet
            item["local_path"] = str(path)
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                bridge_app.LOGGER.warning(
                    "could not remove partial attachment download %s", tmp_path
                )
            item["error"] = str(exc)[:1000]
            bridge_app.record_attachment_history(
                email_id=email_id, raw_attachment=attachment, item=item
            )
            continue

        bridge_app.record_attachment_history(
            email_id=email_id, raw_attachment=attachment, item=item
        )

    return results


async def fetch_and_record_inbound(
    event: dict[str, Any],
    svix_id: str,
    email_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    event_data = event.get("data") or {}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        email = await resend_client.fetch_received_email(
            client,
            email_id,
            api_key=bridge_app.SETTINGS.resend_api_key,
        )
        attachments = await resend_client.fetch_received_attachments(
            client,
            email_id,
            api_key=bridge_app.SETTINGS.resend_api_key,
        )
        if not attachments:
            attachments = (
                email.get("attachments") or event_data.get("attachments") or []
            )

    to_bot = is_to_inbound_address(email, event_data)
    bridge_app.record_inbound_email(
        svix_id=svix_id,
        event=event,
        email=email,
        attachments=attachments,
        addressed_to_inbound=to_bot,
    )
    record_fetched_attachment_metadata(email_id, attachments)
    bridge_app.record_processing_step(
        step="fetch_email",
        status=bridge_app.StepStatus.DONE,
        svix_id=svix_id or None,
        email_id=email_id,
        detail={"attachment_count": len(attachments), "addressed_to_inbound": to_bot},
    )
    return email, attachments, to_bot


async def notify_non_bot_email(
    email_id: str,
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> None:
    bridge_app.LOGGER.info(
        "email %s is not addressed to %s; forwarding to Telegram",
        email_id,
        bridge_app.SETTINGS.inbound_address,
    )
    notice = render_inbound_email_notice(
        email,
        attachments,
        title="主人你有一封新邮件~",
        domain=bridge_app.SETTINGS.resend_domain,
    )
    downloaded = await download_attachments_for_notification(email_id, attachments)
    await bridge_app.notify_telegram(
        notice,
        email_id=email_id,
        attachment_paths=notification_attachment_paths(downloaded),
    )

    bridge_app.update_inbound_status(email_id, bridge_app.InboundStatus.NOTIFIED)


async def notify_bot_email_received(
    email_id: str,
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> None:
    """Immediately show the original bot-addressed email before processing it."""

    title = bridge_app.NOTIFICATION_BOT_TITLE.format(AI_NAME=bridge_app.SETTINGS.ai_name)
    downloaded = await download_attachments_for_notification(email_id, attachments)
    await bridge_app.notify_telegram(
        render_inbound_email_notice(
            email,
            attachments,
            title=title,
            domain=bridge_app.SETTINGS.resend_domain,
        ),
        email_id=email_id,
        attachment_paths=notification_attachment_paths(downloaded),
    )
    bridge_app.update_inbound_status(email_id, bridge_app.InboundStatus.PROCESSING)


async def decide_bot_email(
    email_id: str,
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        downloaded = await download_relevant_attachments(client, email_id, attachments)

    bridge_app.LOGGER.info(
        "email %s is addressed to %s; running Hermes task",
        email_id,
        bridge_app.SETTINGS.inbound_address,
    )
    decision = await run_hermes_email_task(
        email=email,
        attachments=attachments,
        downloaded=downloaded,
        email_id=email_id,
    )
    decision["_downloaded_files"] = downloaded
    return decision


async def handle_hermes_decision(
    email_id: str,
    email: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    if not isinstance(decision, dict):
        decision = bridge_app.fallback_notify_decision(
            str(decision),
            "Hermes decision was missing or not a JSON object.",
        )
    action = str(decision.get("action", "notify")).lower()
    reply_payload: dict[str, Any] | None = None
    reply_id: str | None = None
    if action == "reply" and not reply_text_from_decision(decision):
        decision["action"] = "notify"
        decision["owner_report"] = (
            str(decision.get("owner_report") or "").strip()
            + " Hermes chose reply but did not provide a reply body, so the bridge skipped the email reply."
        ).strip()
        action = "notify"
    if action == "reply":
        bridge_app.LOGGER.info("Hermes chose reply for email %s", email_id)
        reply_payload = build_resend_reply_payload(email, decision)
        reply_id = await send_resend_reply(
            email,
            decision,
            email_id=email_id,
            reply_payload=reply_payload,
        )
        await bridge_app.notify_telegram(
            render_processing_result_notice(
                "Hermes 已通过 Resend 自动回复。",
                decision=decision,
                domain=bridge_app.SETTINGS.resend_domain,
                reply_payload=reply_payload,
                reply_id=reply_id,
            ),
            email_id=email_id,
        )
        bridge_app.update_inbound_status(email_id, bridge_app.InboundStatus.REPLIED)
    else:
        bridge_app.LOGGER.info("Hermes chose no email reply for email %s", email_id)
        bridge_app.update_inbound_status(email_id, bridge_app.InboundStatus.NOTIFIED)

    owner_report_attachments = _validate_agent_attachment_paths(
        bridge_app.ensure_list(decision.get("owner_report_attachments") or [])
    )

    await bridge_app.notify_telegram(
        build_activity_summary(decision),
        email_id=email_id,
        attachment_paths=owner_report_attachments,
    )


async def process_event(event: dict[str, Any], svix_id: str) -> None:
    if not bridge_app.SETTINGS.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")

    event_data = event.get("data") or {}
    email_id = str(event_data["email_id"])
    email, attachments, to_bot = await fetch_and_record_inbound(
        event, svix_id, email_id
    )

    if not to_bot:
        await notify_non_bot_email(email_id, email, attachments)
        return

    await notify_bot_email_received(email_id, email, attachments)
    bridge_app._write_bot_reply_context(
        email_id,
        sender=bridge_app.bot_sender_address(),
        reply_to=str(email.get("from") or ""),
    )
    try:
        decision = await decide_bot_email(email_id, email, attachments)
        await handle_hermes_decision(email_id, email, decision)
    finally:
        bridge_app._delete_bot_reply_context(email_id)


async def process_event_safe(event: dict[str, Any], svix_id: str) -> None:
    email_id = str((event.get("data") or {}).get("email_id") or "")
    try:
        await process_event(event, svix_id)
        bridge_app.mark_event_done(svix_id)
        bridge_app.record_processing_step(
            step="event",
            status=bridge_app.StepStatus.DONE,
            svix_id=svix_id,
            email_id=email_id or None,
        )
    except Exception as exc:
        bridge_app.LOGGER.exception("failed to process Resend inbound event")
        bridge_app.mark_event_failed(svix_id, exc)
        if email_id:
            bridge_app.update_inbound_status(
                email_id, bridge_app.InboundStatus.FAILED, str(exc)[:1000]
            )
        bridge_app.record_processing_step(
            step="event",
            status=bridge_app.StepStatus.FAILED,
            svix_id=svix_id,
            email_id=email_id or None,
            error=str(exc)[:1000],
        )
        await bridge_app.notify_telegram(
            "Resend inbound processing failed.\n"
            f"Email ID: {email_id or None}\n"
            f"Error: {exc}",
            email_id=email_id or None,
        )


def build_activity_summary(decision: dict[str, Any]) -> str:
    owner_report = str(decision.get("owner_report") or "").strip()
    return f"**任务总结：**\n\n{owner_report}" if owner_report else "**任务总结：**"


def email_summary(email: dict[str, Any]) -> dict[str, Any]:
    text = str(email.get("text") or "")
    html = str(email.get("html") or "")
    return {
        "id": email.get("id"),
        "created_at": email.get("created_at"),
        "from": email.get("from"),
        "to": email.get("to"),
        "cc": email.get("cc"),
        "subject": email.get("subject"),
        "message_id": email.get("message_id"),
        "headers": email.get("headers"),
        "text_preview": text[:8000],
        "html_preview": html[:4000] if not text else "",
    }
