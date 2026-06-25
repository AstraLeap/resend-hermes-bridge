from __future__ import annotations

import asyncio
from typing import Any

from db.state import OutboundStatus, StepStatus
from services.hermes_context import append_notification_to_user_context, parse_notification_target
from services.telegram_rich import send_telegram_rich_text
from utils.email_display import render_email_markdown


def _bridge_app():
    import app as bridge_app

    return bridge_app


async def _communicate_or_kill(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()
        raise


def notification_target_supports_markdown_tables(target: str | None = None) -> bool:
    """All notification targets use the standard Markdown table template."""
    return True


def notification_target_is_telegram(target: str | None) -> bool:
    return parse_notification_target(str(target or "").strip()).platform.lower() == "telegram"


def context_message_for_notification(
    target: str,
    email_id: str | None,
    message: str,
    *,
    kind: str,
    path: str | None = None,
) -> str:
    """Return the notification text as-is for Hermes context.

    The context message should match what the user sees in the chat so the
    model references the same content when the user mentions "刚才" or
    "这封邮件".
    """
    return message


def record_notification_context(
    target: str,
    email_id: str | None,
    message: str,
    *,
    kind: str,
    path: str | None = None,
) -> dict[str, object]:
    bridge_app = _bridge_app()
    try:
        context_message = context_message_for_notification(
            target,
            email_id,
            message,
            kind=kind,
            path=path,
        )
        context_result = append_notification_to_user_context(target, context_message)
        context_payload: dict[str, object] = context_result.as_payload()
        context_payload["kind"] = kind
        if path is not None:
            context_payload["path"] = path
        bridge_app.record_processing_step(
            step=f"{target}_context",
            status=StepStatus.DONE if context_result.recorded else StepStatus.IGNORED,
            email_id=email_id,
            detail=context_payload,
        )
        return context_payload
    except Exception as exc:
        context_payload = {"recorded": False, "kind": kind, "error": str(exc)[:1000]}
        if path is not None:
            context_payload["path"] = path
        bridge_app.record_processing_step(
            step=f"{target}_context",
            status=StepStatus.FAILED,
            email_id=email_id,
            detail=context_payload,
            error=str(exc)[:1000],
        )
        return context_payload


async def send_hermes_notification_text(target: str, message: str) -> tuple[str, str]:
    bridge_app = _bridge_app()
    if not bridge_app.SETTINGS.hermes_send_bin.exists():
        raise RuntimeError(
            f"hermes send binary not found: {bridge_app.SETTINGS.hermes_send_bin}"
        )

    if notification_target_is_telegram(target):
        bridge_app.LOGGER.info(
            "target %s is telegram; attempting rich send", target
        )
        try:
            rich_result = await send_telegram_rich_text(target, message)
            if rich_result.sent:
                bridge_app.LOGGER.info(
                    "telegram rich send succeeded for %s: %s",
                    target,
                    rich_result.stdout.strip(),
                )
                return rich_result.stdout, rich_result.stderr
            if rich_result.reason:
                bridge_app.LOGGER.info(
                    "telegram rich notification skipped: %s", rich_result.reason
                )
        except Exception as exc:
            bridge_app.LOGGER.warning("telegram rich notification fallback: %s", exc)
    else:
        bridge_app.LOGGER.info(
            "target %s is not telegram; skipping rich send", target
        )

    process = await asyncio.create_subprocess_exec(
        str(bridge_app.SETTINGS.hermes_send_bin),
        "send",
        "--to",
        target,
        message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_or_kill(process, timeout=90)
    stdout_text = stdout.decode(errors="replace")
    stderr_text = stderr.decode(errors="replace")
    if process.returncode != 0:
        raise RuntimeError(
            f"failed to send {target} notification: "
            f"stdout={stdout_text} stderr={stderr_text}"
        )
    return stdout_text, stderr_text


async def send_email_display_notification(
    payload: dict[str, Any],
    *,
    title: str | None,
    domain: str,
    draft_id: str | None = None,
    email_id: str | None = None,
    footer: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    attachment_paths: list[str] | None = None,
    target: str | None = None,
    body_limit: int | None = None,
    notice_limit: int | None = 3800,
    show_attachments: bool = True,
) -> str:
    """Render one email display with the standard template, then send it."""
    bridge_app = _bridge_app()
    notice = render_email_markdown(
        payload,
        title=title,
        domain=domain,
        draft_id=draft_id,
        email_id=email_id,
        footer=footer,
        attachments=attachments,
        body_limit=body_limit,
        notice_limit=notice_limit,
        show_attachments=show_attachments,
    )
    send_kwargs: dict[str, Any] = {
        "email_id": email_id,
        "attachment_paths": attachment_paths or [],
    }
    if target is not None:
        send_kwargs["target"] = target
    await bridge_app.send_notification(notice, **send_kwargs)
    return notice


async def send_notification(
    message: str,
    *,
    email_id: str | None = None,
    attachment_paths: list[str] | None = None,
    target: str | None = None,
) -> None:
    bridge_app = _bridge_app()
    attachment_paths = attachment_paths or []
    target = str(target or bridge_app.SETTINGS.notification_target).strip()
    payload = {
        "command": [
            str(bridge_app.SETTINGS.hermes_send_bin),
            "send",
            "--to",
            target,
            message,
        ],
        "attachment_paths": attachment_paths,
    }
    outbound_id = bridge_app.create_outbound_message(
        kind=f"{target}_notification",
        email_id=email_id,
        recipient=target,
        body_text=message,
        payload=payload,
    )
    stdout_text = ""
    stderr_text = ""
    context_records: list[dict[str, object]] = []
    try:
        stdout, stderr = await send_hermes_notification_text(target, message)
        stdout_text += stdout
        stderr_text += stderr

        context_records.append(
            record_notification_context(target, email_id, message, kind="text")
        )

        for path in attachment_paths:
            if not path:
                continue
            media_message = f"MEDIA:{path}"
            media_process = await asyncio.create_subprocess_exec(
                str(bridge_app.SETTINGS.hermes_send_bin),
                "send",
                "--to",
                target,
                media_message,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            media_stdout, media_stderr = await _communicate_or_kill(
                media_process, timeout=90
            )
            stdout_text += f"\n[MEDIA {path}]\n{media_stdout.decode(errors='replace')}"
            stderr_text += f"\n[MEDIA {path}]\n{media_stderr.decode(errors='replace')}"
            if media_process.returncode != 0:
                raise RuntimeError(
                    f"failed to send {target} media {path}: "
                    f"stdout={stdout_text} stderr={stderr_text}"
                )
            context_records.append(
                record_notification_context(
                    target,
                    email_id,
                    media_message,
                    kind="media",
                    path=path,
                )
            )
    except Exception as exc:
        bridge_app.update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
            response={"context": context_records} if context_records else None,
            stdout=stdout_text,
            stderr=stderr_text,
            error=str(exc)[:1000],
        )
        bridge_app.record_processing_step(
            step=f"{target}_notify",
            status=StepStatus.FAILED,
            email_id=email_id,
            error=str(exc)[:1000],
        )
        raise

    bridge_app.update_outbound_message(
        outbound_id,
        status=OutboundStatus.SENT,
        stdout=stdout_text,
        stderr=stderr_text,
        response={"context": context_records} if context_records else None,
    )
    bridge_app.record_processing_step(
        step=f"{target}_notify",
        status=StepStatus.SENT,
        email_id=email_id,
    )
