from __future__ import annotations

import re
from email.utils import formataddr, parseaddr
from typing import Any

EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}$")


class EmailValidationError(ValueError):
    """Raised when a user-supplied email field is invalid."""


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def clean_header_value(
    value: Any,
    label: str,
    *,
    required: bool = False,
    limit: int = 998,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise EmailValidationError(f"{label} is required")
    if "\r" in text or "\n" in text:
        raise EmailValidationError(f"{label} cannot contain newlines")
    if len(text) > limit:
        raise EmailValidationError(f"{label} is too long")
    return text


def parse_email_address(value: Any, label: str) -> str:
    raw = clean_header_value(value, label, required=True, limit=320)
    address = parseaddr(raw)[1] or raw
    if "\r" in address or "\n" in address or "@" not in address:
        raise EmailValidationError(f"{label} must be a valid email address")
    local, domain = address.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise EmailValidationError(f"{label} must be a valid email address")
    return address


def parse_email_addresses(
    value: Any,
    label: str,
    *,
    required: bool = False,
) -> list[str]:
    items = [item for item in ensure_list(value) if str(item or "").strip()]
    if required and not items:
        raise EmailValidationError(f"{label} is required")
    if len(items) > 50:
        raise EmailValidationError(f"{label} has too many recipients")
    return [parse_email_address(item, label) for item in items]


def clean_from_local(value: Any) -> str | None:
    local = str(value or "").strip()
    if not local:
        return None
    if "@" in local or not EMAIL_LOCAL_RE.match(local):
        raise EmailValidationError("from_local must be a valid local part, e.g. bot or notice")
    return local


def default_from_local(
    to_addresses: list[str],
    *,
    domain: str,
    bot_local: str | None = None,
    owner_local: str | None = None,
) -> str:
    import os

    bot_local = (bot_local or os.getenv("BOT_FROM_LOCAL") or "").strip().lower()
    owner_local = (owner_local or os.getenv("OWNER_FROM_LOCAL") or "").strip().lower()
    if not bot_local or not owner_local:
        raise RuntimeError("BOT_FROM_LOCAL and OWNER_FROM_LOCAL are required.")
    normalized = {address.lower() for address in to_addresses}
    return bot_local if f"{bot_local}@{domain.lower()}" in normalized else owner_local


def resolve_sender(
    raw: dict[str, Any],
    *,
    domain: str,
    default_from: str,
) -> str:
    from_email = clean_header_value(raw.get("from_email"), "from_email", limit=320)
    from_local = clean_header_value(raw.get("from_local"), "from_local", limit=64)
    from_name = clean_header_value(raw.get("from_name"), "from_name", limit=120)
    if from_email and from_local:
        raise EmailValidationError("provide from_email or from_local, not both")

    normalized_domain = domain.lower()
    if from_email:
        address = parse_email_address(from_email, "from_email")
        sender_domain = address.rsplit("@", 1)[1].lower()
        if sender_domain != normalized_domain:
            raise EmailValidationError(f"from_email must use @{normalized_domain}")
    elif from_local:
        local = clean_from_local(from_local)
        address = f"{local}@{normalized_domain}"
    else:
        address = parseaddr(default_from)[1] or default_from

    if from_name:
        return formataddr((from_name, address))
    return address


def email_address_list(email: dict[str, Any], key: str) -> list[str]:
    return [str(item) for item in ensure_list(email.get(key))]


def outbound_recipient_summary(payload: dict[str, Any]) -> str:
    recipients: list[str] = []
    for key in ("to", "cc", "bcc"):
        recipients.extend(str(item) for item in ensure_list(payload.get(key)))
    return ", ".join(recipients)
