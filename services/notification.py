from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from db.state import OutboundStatus, StepStatus
from services.hermes_context import append_notification_to_user_context, parse_notification_target
from utils.email_display import render_email_markdown


def _bridge_app():
    import app as bridge_app

    return bridge_app


# This script is executed inside the Hermes virtualenv so it can import and call
# plugins.platforms.telegram.adapter.TelegramAdapter.send() directly.  It is a
# thin caller: all Telegram-specific sending logic stays in Hermes code.
_TELEGRAM_ADAPTER_SCRIPT = '''
import asyncio, json, os, sys

try:
    payload = json.load(sys.stdin)
except Exception as exc:
    print(json.dumps({"error": f"bad input: {exc}"}), flush=True)
    os._exit(1)

message = payload.get("message", "")
chat_id = payload.get("chat_id") or ""
thread_id = payload.get("thread_id") or ""
hermes_home = payload.get("hermes_home") or os.path.expanduser("~/.hermes")

sys.path.insert(0, os.path.join(hermes_home, "hermes-agent"))

async def _send():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(hermes_home, ".env"), override=True)

    from gateway.config import load_gateway_config, Platform
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from gateway.platforms.base import resolve_proxy_url

    cfg = load_gateway_config()
    pconfig = cfg.platforms.get(Platform.TELEGRAM)
    if not pconfig or not pconfig.token:
        return {"error": "Telegram platform is not configured with a token"}

    if not chat_id:
        home = cfg.get_home_channel(Platform.TELEGRAM)
        if not home:
            return {"error": "No chat_id provided and no Telegram home channel configured"}
        _chat_id = home.chat_id
        _thread_id = thread_id or home.thread_id
    else:
        _chat_id = chat_id
        _thread_id = thread_id

    bot_kwargs = {}
    extra = pconfig.extra or {}
    base_url = extra.get("base_url")
    if base_url:
        bot_kwargs["base_url"] = base_url
        bot_kwargs["base_file_url"] = extra.get("base_file_url", base_url)
    if extra.get("local_mode"):
        bot_kwargs["local_mode"] = True

    proxy_url = resolve_proxy_url("TELEGRAM_PROXY", target_hosts=["api.telegram.org"])
    if proxy_url:
        bot_kwargs["request"] = HTTPXRequest(proxy=proxy_url)

    bot = Bot(pconfig.token, **bot_kwargs)
    adapter = TelegramAdapter(pconfig)
    adapter._bot = bot

    metadata = {"notify": True}
    if _thread_id:
        metadata["thread_id"] = str(_thread_id)

    result = await adapter.send(str(_chat_id), message, metadata=metadata)
    if not result.success:
        return {
            "error": result.error or "Telegram adapter send failed",
            "retryable": bool(getattr(result, "retryable", False)),
        }
    return {"success": True, "message_id": result.message_id}

try:
    print(json.dumps(asyncio.run(_send())), flush=True)
except Exception as exc:
    print(json.dumps({"error": str(exc)}), flush=True)
os._exit(0)
'''


def _is_telegram_target(target: str) -> bool:
    return parse_notification_target(target).platform == "telegram"


async def _send_telegram_adapter_notification(
    target: str,
    message: str,
    *,
    hermes_home: Path,
    hermes_venv_python_bin: Path,
    timeout: float = 90,
) -> tuple[str, str]:
    """Send a Telegram notification by calling Hermes's TelegramAdapter.

    The actual adapter code runs inside the Hermes virtualenv subprocess so
    that the bridge does not need to duplicate python-telegram-bot or adapter
    dependencies.
    """
    parsed = parse_notification_target(target)
    payload = {
        "message": message,
        "chat_id": parsed.chat_id or "",
        "thread_id": parsed.thread_id or "",
        "hermes_home": str(hermes_home),
    }

    if not hermes_venv_python_bin.exists():
        raise RuntimeError(
            f"Hermes virtualenv Python not found: {hermes_venv_python_bin}"
        )

    process = await asyncio.create_subprocess_exec(
        str(hermes_venv_python_bin),
        "-c",
        _TELEGRAM_ADAPTER_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    input_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stdout, stderr = await _communicate_or_kill(
        process,
        input=input_bytes,
        timeout=timeout,
    )
    stdout_text = stdout.decode(errors="replace")
    stderr_text = stderr.decode(errors="replace")

    if process.returncode != 0:
        raise RuntimeError(
            f"telegram adapter send failed (exit {process.returncode}): "
            f"stdout={stdout_text} stderr={stderr_text}"
        )

    last_line = stdout_text.strip().splitlines()[-1] if stdout_text.strip() else ""
    try:
        result = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"telegram adapter returned unexpected output: "
            f"stdout={stdout_text} stderr={stderr_text}"
        ) from exc

    if result.get("error"):
        raise RuntimeError(
            f"telegram adapter send failed: {result['error']} "
            f"stdout={stdout_text} stderr={stderr_text}"
        )
    return stdout_text, stderr_text


async def _communicate_or_kill(
    process: asyncio.subprocess.Process,
    *,
    input: bytes | None = None,
    timeout: float,
) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(process.communicate(input=input), timeout=timeout)
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
    prefer_html_body: bool = False,
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
        prefer_html_body=prefer_html_body,
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

    if _is_telegram_target(target):
        payload: dict[str, Any] = {
            "mode": "telegram_adapter",
            "target": target,
            "attachment_paths": attachment_paths,
        }
    else:
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
        if _is_telegram_target(target):
            stdout, stderr = await _send_telegram_adapter_notification(
                target,
                message,
                hermes_home=bridge_app.SETTINGS.hermes_home,
                hermes_venv_python_bin=bridge_app.SETTINGS.hermes_venv_python_bin,
            )
        else:
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
