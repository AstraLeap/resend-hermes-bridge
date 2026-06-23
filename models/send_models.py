from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
