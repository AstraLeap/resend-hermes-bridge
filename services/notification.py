from __future__ import annotations

import asyncio

import app as bridge_app
from db.state import OutboundStatus, StepStatus


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
    try:
        stdout, stderr = await send_hermes_notification_text(target, message)
        stdout_text += stdout
        stderr_text += stderr

        for path in attachment_paths:
            if not path:
                continue
            host_path = bridge_app.host_path_for_bridge_path(path)
            media_message = f"MEDIA:{host_path}"
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
            stdout_text += f"\n[MEDIA {host_path}]\n{media_stdout.decode(errors='replace')}"
            stderr_text += f"\n[MEDIA {host_path}]\n{media_stderr.decode(errors='replace')}"
            if media_process.returncode != 0:
                raise RuntimeError(
                    f"failed to send {target} media {host_path}: "
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
