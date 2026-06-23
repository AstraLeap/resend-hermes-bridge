from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from dotenv import load_dotenv

import app as bridge_app
from db.state import OutboundStatus, StepStatus


def notification_platform(target: str) -> str:
    return str(target or "").split(":", 1)[0].strip().lower()


def is_telegram_notification_target(target: str) -> bool:
    return notification_platform(target) == "telegram"


def load_hermes_messaging_env() -> None:
    env_path = bridge_app._hermes_home() / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True, encoding="utf-8")


def telegram_rich_destination(target: str) -> tuple[str, str | None]:
    load_hermes_messaging_env()
    parts = str(target or "").split(":")
    chat_id = ""
    thread_id: str | None = None
    if len(parts) >= 2 and parts[1].strip():
        chat_id = parts[1].strip()
        if len(parts) >= 3 and parts[2].strip():
            thread_id = parts[2].strip()
    else:
        chat_id = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
        thread_id = os.getenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "").strip() or None

    if not chat_id:
        raise RuntimeError("Telegram chat target is not configured")
    if chat_id.startswith("#"):
        raise RuntimeError("Telegram channel-name targets require hermes send")
    return chat_id, thread_id


def telegram_api_chat_id(chat_id: str) -> int | str:
    stripped = chat_id.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def telegram_api_thread_id(thread_id: str | None) -> int | None:
    if not thread_id:
        return None
    stripped = str(thread_id).strip()
    if not stripped or stripped == "1":
        return None
    if not stripped.isdigit():
        raise RuntimeError("Telegram thread target must be numeric")
    return int(stripped)


async def send_telegram_rich_notification(message: str, *, target: str) -> str:
    load_hermes_messaging_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    chat_id, thread_id = telegram_rich_destination(target)
    payload: dict[str, Any] = {
        "chat_id": telegram_api_chat_id(chat_id),
        "rich_message": {"markdown": message},
    }
    api_thread_id = telegram_api_thread_id(thread_id)
    if api_thread_id is not None:
        payload["message_thread_id"] = api_thread_id

    url = f"https://api.telegram.org/bot{token}/sendRichMessage"
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, json=payload)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or body.get("ok") is False:
        description = body.get("description") or response.text
        raise RuntimeError(f"Telegram sendRichMessage failed: {description}")

    result = body.get("result") if isinstance(body, dict) else None
    message_id = ""
    if isinstance(result, dict):
        message_id = str(result.get("message_id") or "")
    suffix = f" (message_id: {message_id})" if message_id else ""
    return f"Sent rich Markdown table to telegram chat {chat_id}{suffix}\n"


async def send_hermes_notification_text(target: str, message: str) -> tuple[str, str]:
    if not bridge_app.SETTINGS.hermes_send_bin.exists():
        raise RuntimeError(
            f"hermes send binary not found: {bridge_app.SETTINGS.hermes_send_bin}"
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
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    stdout_text = stdout.decode(errors="replace")
    stderr_text = stderr.decode(errors="replace")
    if process.returncode != 0:
        raise RuntimeError(
            f"failed to send {target} notification: "
            f"stdout={stdout_text} stderr={stderr_text}"
        )
    return stdout_text, stderr_text


async def notify_telegram(
    message: str,
    *,
    email_id: str | None = None,
    attachment_paths: list[str] | None = None,
) -> None:
    attachment_paths = attachment_paths or []
    target = bridge_app.SETTINGS.notification_target
    payload = {
        "command": [str(bridge_app.SETTINGS.hermes_send_bin), "send", "--to", target, message],
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
    sent_text_message = False
    try:
        if is_telegram_notification_target(target):
            try:
                stdout_text = await send_telegram_rich_notification(
                    message,
                    target=target,
                )
                sent_text_message = True
            except Exception as exc:
                stderr_text = (
                    f"Telegram rich notification failed; falling back to hermes send: "
                    f"{bridge_app.exception_message(exc)}"
                )

        if not sent_text_message:
            stdout, stderr = await send_hermes_notification_text(target, message)
            stdout_text += stdout
            stderr_text += stderr

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
            media_stdout, media_stderr = await asyncio.wait_for(
                media_process.communicate(), timeout=90
            )
            stdout_text += f"\n[MEDIA {path}]\n{media_stdout.decode(errors='replace')}"
            stderr_text += f"\n[MEDIA {path}]\n{media_stderr.decode(errors='replace')}"
            if media_process.returncode != 0:
                raise RuntimeError(
                    f"failed to send {target} media {path}: "
                    f"stdout={stdout_text} stderr={stderr_text}"
                )
    except Exception as exc:
        bridge_app.update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
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
    )
    bridge_app.record_processing_step(
        step=f"{target}_notify",
        status=StepStatus.SENT,
        email_id=email_id,
    )
