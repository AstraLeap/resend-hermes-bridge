from __future__ import annotations

import base64
import binascii
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

import app as bridge_app
import services.resend_client as resend_client
from db.state import OutboundStatus, StepStatus
from utils.email_core import (
    clean_header_value,
    ensure_list,
    outbound_recipient_summary,
)


class AttachmentSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str | None = None
    local_path: str | None = None
    filename: str | None = None
    content: str | None = None
    content_type: str | None = None
    content_id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> AttachmentSpec:
        has_path = bool((self.path or self.local_path or "").strip())
        has_content = self.content not in (None, "")
        if has_path and has_content:
            raise ValueError("attachment must provide path or content, not both")
        return self


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    confirmed: bool | None = None
    draft_id: str | None = None
    approval_token: str | None = None
    auto_reply_email_id: str | None = None
    email_id: str | None = None
    from_email: str | None = None
    from_local: str | None = None
    from_name: str | None = None
    to: list[str] | str | None = None
    cc: list[str] | str | None = None
    bcc: list[str] | str | None = None
    reply_to: list[str] | str | None = None
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    headers: dict[str, Any] | None = None
    attachments: list[AttachmentSpec | str] | AttachmentSpec | str | None = None

    def raw_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python", exclude_none=True)
        extras = self.model_extra or {}
        payload.update(extras)
        return payload


class HermesDecision(BaseModel):
    action: str = Field(default="notify")
    executed_task: bool = False
    owner_report: str = ""
    owner_report_attachments: list[Any] = Field(default_factory=list)
    reply_subject: str = ""
    reply_text: str = ""
    reply_attachments: list[Any] = Field(default_factory=list)


def _decoded_base64_size(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    compact = re.sub(r"\s+", "", text)
    padding = len(compact) - len(compact.rstrip("="))
    return max((len(compact) * 3) // 4 - padding, 0)


def outbound_attachment_audit_metadata(attachment: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("filename", "content_type", "content_id", "path"):
        value = attachment.get(key)
        if value not in (None, "", []):
            metadata[key] = value
    if attachment.get("content"):
        metadata["content_bytes"] = _decoded_base64_size(attachment.get("content"))
        metadata["content_redacted"] = True
    return metadata


def outbound_payload_for_audit(payload: dict[str, Any]) -> dict[str, Any]:
    audit_payload = dict(payload)
    attachments = payload.get("attachments")
    if attachments:
        audit_payload["attachments"] = [
            outbound_attachment_audit_metadata(item)
            for item in ensure_list(attachments)
            if isinstance(item, dict)
        ]
    return audit_payload


def normalize_outbound_attachments(
    raw_attachments: Any,
    *,
    email_id: str = "",
    restrict_to_inbound: bool = False,
) -> list[dict[str, Any]]:
    if raw_attachments in (None, "", []):
        return []

    items = [
        item for item in ensure_list(raw_attachments) if item not in (None, "", [])
    ]
    if len(items) > bridge_app.MAX_OUTBOUND_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"attachments has too many items; max {bridge_app.MAX_OUTBOUND_ATTACHMENTS}",
        )

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    inbound_root: Path | None = None
    if restrict_to_inbound:
        if not email_id:
            raise HTTPException(
                status_code=400,
                detail="auto-reply attachments require auto_reply_email_id",
            )
        inbound_root = bridge_app.bridge_inbound_attachment_dir(email_id).resolve()

    for index, item in enumerate(items):
        if isinstance(item, str):
            spec: dict[str, Any] = {"path": item}
        elif isinstance(item, dict):
            spec = dict(item)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"attachments[{index}] must be an object or local path string",
            )

        raw_path = clean_header_value(
            spec.get("path") or spec.get("local_path"),
            f"attachments[{index}].path",
            limit=4096,
        )
        raw_content = spec.get("content")
        if restrict_to_inbound and not raw_path:
            raise HTTPException(
                status_code=400,
                detail="authorized bot auto-reply attachments must use downloaded attachment paths",
            )
        if raw_path and raw_content not in (None, ""):
            raise HTTPException(
                status_code=400,
                detail=f"attachments[{index}] must provide path or content, not both",
            )

        filename = clean_header_value(
            spec.get("filename"), f"attachments[{index}].filename", limit=255
        )
        content_type = clean_header_value(
            spec.get("content_type"), f"attachments[{index}].content_type", limit=120
        )
        content_id = clean_header_value(
            spec.get("content_id"), f"attachments[{index}].content_id", limit=256
        )

        if raw_path:
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = bridge_app.APP_DIR / path
            path = path.resolve()
            if inbound_root is not None:
                allowed_roots = [inbound_root] + [
                    root.resolve() for root in bridge_app.GENERATED_ATTACHMENT_ROOTS
                ]
                if not any(
                    bridge_app._path_is_relative_to(path, root)
                    for root in allowed_roots
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="authorized bot auto-reply attachments must come from this inbound email or generated file directories",
                    )
            if not path.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"attachment path does not exist or is not a file: {raw_path}",
                )
            file_size = path.stat().st_size
            if file_size == 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}] is empty; Resend does not support zero-byte attachments",
                )
            if file_size > bridge_app.SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}] is larger than {bridge_app.SETTINGS.max_outbound_attachment_bytes} bytes",
                )
            total_bytes += file_size
            if total_bytes > bridge_app.SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments total is larger than {bridge_app.SETTINGS.max_outbound_attachment_bytes} bytes",
                )
            filename = filename or path.name
            try:
                content = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as exc:
                raise HTTPException(
                    status_code=400, detail=f"could not read attachment: {raw_path}"
                ) from exc
        else:
            if raw_content in (None, ""):
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}] requires path or base64 content",
                )
            try:
                decoded = base64.b64decode(str(raw_content), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}].content must be base64",
                ) from exc
            file_size = len(decoded)
            if file_size > bridge_app.SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}] is larger than {bridge_app.SETTINGS.max_outbound_attachment_bytes} bytes",
                )
            total_bytes += file_size
            if total_bytes > bridge_app.SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments total is larger than {bridge_app.SETTINGS.max_outbound_attachment_bytes} bytes",
                )
            content = base64.b64encode(decoded).decode("ascii")

        filename = Path(filename).name if filename else ""
        if not filename:
            raise HTTPException(
                status_code=400, detail=f"attachments[{index}].filename is required"
            )

        attachment: dict[str, Any] = {
            "filename": filename,
            "content": content,
        }
        if content_type:
            attachment["content_type"] = content_type
        if content_id:
            attachment["content_id"] = content_id
        normalized.append(attachment)

    return normalized


async def send_resend_email(
    payload: dict[str, Any],
    *,
    email_id: str | None = None,
    step: str = "resend_send",
) -> tuple[int, str]:
    outbound_id = bridge_app.create_outbound_message(
        kind="resend_email",
        email_id=email_id,
        recipient=outbound_recipient_summary(payload),
        subject=str(payload.get("subject") or ""),
        body_text=str(payload.get("text") or payload.get("html") or ""),
        payload=outbound_payload_for_audit(payload),
    )
    response_body: Any | None = None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response_body = await resend_client.send_email(
                client,
                payload,
                api_key=bridge_app.SETTINGS.resend_api_key,
                user_agent=bridge_app.USER_AGENT,
            )
        resend_id = str(response_body.get("id") or "")
        bridge_app.update_outbound_message(
            outbound_id,
            status=OutboundStatus.SENT,
            response=response_body,
            external_id=resend_id,
        )
        bridge_app.record_processing_step(
            step=step,
            status=StepStatus.SENT,
            email_id=email_id,
            detail={"resend_id": resend_id, "outbound_id": outbound_id},
        )
        return outbound_id, resend_id
    except resend_client.ResendAPIError as exc:
        response_body = exc.response_body
        bridge_app.update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
            response=response_body,
            error=str(exc)[:1000],
        )
        bridge_app.record_processing_step(
            step=step,
            status=StepStatus.FAILED,
            email_id=email_id,
            error=str(exc)[:1000],
        )
        raise
    except Exception as exc:
        bridge_app.update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
            response=response_body,
            error=str(exc)[:1000],
        )
        bridge_app.record_processing_step(
            step=step,
            status=StepStatus.FAILED,
            email_id=email_id,
            error=str(exc)[:1000],
        )
        raise


async def send_resend_reply(
    email: dict[str, Any],
    decision: dict[str, Any],
    email_id: str,
    reply_payload: dict[str, Any] | None = None,
) -> str:
    raw_payload = dict(reply_payload or build_resend_reply_payload(email, decision))
    raw_payload["confirmed"] = True
    raw_payload["auto_reply_email_id"] = email_id
    payload = bridge_app.normalize_send_payload(raw_payload, allow_bot_sender=True)
    _, reply_id = await send_resend_email(
        payload, email_id=email_id, step="resend_reply"
    )
    return reply_id


def build_resend_reply_payload(
    email: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    sender = parseaddr(str(email.get("from") or ""))[1] or str(email.get("from") or "")
    if not sender:
        raise RuntimeError("cannot reply because the inbound From address is empty")

    original_subject = str(email.get("subject") or "").strip()
    subject = str(decision.get("reply_subject") or "").strip()
    if not subject:
        subject = original_subject
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    if not subject:
        subject = "Re: your email"

    message_id = str(email.get("message_id") or "").strip()
    references = str((email.get("headers") or {}).get("references") or "").strip()
    headers: dict[str, str] = {}
    if message_id:
        headers["In-Reply-To"] = message_id
        headers["References"] = f"{references} {message_id}".strip()

    payload = {
        "from_local": "bot",
        "to": [sender],
        "subject": subject,
        "text": reply_text_from_decision(decision),
        "headers": headers,
    }
    attachments = reply_attachment_specs_from_decision(decision)
    if attachments:
        payload["attachments"] = attachments
    return payload


def reply_text_from_decision(decision: dict[str, Any]) -> str:
    return str(decision.get("reply_text") or decision.get("owner_report") or "").strip()


def reply_attachment_specs_from_decision(
    decision: dict[str, Any],
) -> list[dict[str, Any]]:
    downloaded = [
        item
        for item in ensure_list(decision.get("_downloaded_files"))
        if isinstance(item, dict) and item.get("local_path")
    ]
    by_path = {str(item.get("local_path")): item for item in downloaded}
    by_filename = {
        str(item.get("filename") or ""): item
        for item in downloaded
        if str(item.get("filename") or "")
    }
    by_id = {
        str(item.get("id") or ""): item
        for item in downloaded
        if str(item.get("id") or "")
    }
    selected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    skipped: list[str] = []

    def append_skip(raw: str, reason: str) -> None:
        message = f"{raw} ({reason})"
        skipped.append(message)
        bridge_app.LOGGER.warning("skipping invalid reply attachment: %s", message)

    def append_owner_report_note() -> None:
        if not skipped:
            return
        note = "自动回复时跳过了无效附件：" + "；".join(skipped)
        owner_report = str(decision.get("owner_report") or "").strip()
        if note not in owner_report:
            decision["owner_report"] = f"{owner_report}\n\n{note}".strip()

    def materialize_missing_text_attachment(path: Path) -> bool:
        if path.suffix.lower() not in {".txt", ".md"}:
            return False
        text = str(decision.get("owner_report") or decision.get("reply_text") or "").strip()
        if not text:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            bridge_app.LOGGER.warning(
                "could not create missing reply attachment %s: %s", path, exc
            )
            return False
        return True

    def add_generated_path(
        raw_path: str,
        *,
        filename: str = "",
        content_type: str = "",
        content_id: str = "",
    ) -> None:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = bridge_app.APP_DIR / path
        path = path.resolve()
        if not any(
            bridge_app._path_is_relative_to(path, root)
            for root in bridge_app.GENERATED_ATTACHMENT_ROOTS
        ):
            append_skip(raw_path, "outside generated attachment directories")
            return
        if not path.is_file() and not materialize_missing_text_attachment(path):
            append_skip(raw_path, "not found")
            return
        key = str(path)
        if key in seen_paths:
            return
        spec: dict[str, Any] = {"path": key}
        if filename:
            spec["filename"] = filename
        if content_type:
            spec["content_type"] = content_type
        if content_id:
            spec["content_id"] = content_id
        selected.append(spec)
        seen_paths.add(key)

    def add_downloaded(item: dict[str, Any]) -> None:
        path = str(item.get("local_path") or "")
        if not path or path in seen_paths:
            return
        if not Path(path).expanduser().is_file():
            append_skip(path, "downloaded file not found")
            return
        spec: dict[str, Any] = {"path": path}
        if item.get("filename"):
            spec["filename"] = str(item["filename"])
        if item.get("content_type"):
            spec["content_type"] = str(item["content_type"])
        selected.append(spec)
        seen_paths.add(path)

    for request in ensure_list(decision.get("reply_attachments")):
        if request in (None, "", []):
            continue
        if isinstance(request, dict):
            path = str(request.get("path") or request.get("local_path") or "")
            filename = str(request.get("filename") or "")
            attachment_id = str(request.get("id") or "")
            matched = (
                by_path.get(path)
                or by_filename.get(filename)
                or by_id.get(attachment_id)
            )
            if matched:
                add_downloaded(matched)
                continue
            if path and path not in seen_paths:
                add_generated_path(
                    path,
                    filename=filename,
                    content_type=str(request.get("content_type") or ""),
                    content_id=str(request.get("content_id") or ""),
                )
            continue

        key = str(request)
        matched = by_path.get(key) or by_filename.get(key) or by_id.get(key)
        if matched:
            add_downloaded(matched)
            continue
        if key and key not in seen_paths:
            add_generated_path(key)

    append_owner_report_note()
    return selected
