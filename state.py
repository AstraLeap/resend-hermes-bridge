from __future__ import annotations

from enum import StrEnum


class EventStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class InboundStatus(StrEnum):
    FETCHED = "fetched"
    PROCESSING = "processing"
    NOTIFIED = "notified"
    REPLIED = "replied"
    FAILED = "failed"


class StepStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"
    IGNORED = "ignored"
    QUEUED = "queued"
    DUPLICATE = "duplicate"
    SCHEDULED = "scheduled"
    SENT = "sent"


class OutboundStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    SENT = "sent"
    FAILED = "failed"
