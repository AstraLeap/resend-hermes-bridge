from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

import settings as bridge_settings
from services.hermes_context import parse_notification_target

LOGGER = logging.getLogger("resend-hermes-bridge.telegram_rich")
RICH_MESSAGE_MAX_CHARS = 32768

_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$"
)
_RICH_DETAILS_RE = re.compile(r"<details\b[^>]*>.*?</details>", re.IGNORECASE | re.DOTALL)
_RICH_MATH_IN_DETAILS_RE = re.compile(
    r"(\$\$.*?\$\$|"
    r"\\\[.*?\\\]|"
    r"\\\(.*?\\\)|"
    r"\\(?:sum|frac|alpha|beta|gamma|delta|theta|lambda|mu|pi|sigma|"
    r"int|prod|sqrt|lim|infty|begin\{(?:equation|align|matrix|cases)\}))",
    re.IGNORECASE | re.DOTALL,
)
_RICH_PROTECTED_REGION_RE = re.compile(
    r"(?:```[^\n]*\n[\s\S]*?```)"
    r"|(?:^[^\n]*\|[^\n]*\n"
    r"[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*"
    r"(?:\n[^\n]*\|[^\n]*)*)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TelegramRichSendResult:
    sent: bool
    stdout: str = ""
    stderr: str = ""
    reason: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class TelegramRichConfig:
    token: str | None
    chat_id: str | None
    thread_id: str | None
    extra: dict[str, Any]


def _read_hermes_config_yaml() -> dict[str, Any]:
    config_file = bridge_settings.hermes_home() / "config.yaml"
    if not config_file.exists():
        return {}
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _deep_get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _merge_telegram_extra(config: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in (
        ("gateway", "platforms", "telegram", "extra"),
        ("platforms", "telegram", "extra"),
        ("telegram", "extra"),
    ):
        value = _deep_get(config, path)
        if isinstance(value, dict):
            merged.update(value)
    return merged


def _telegram_home_from_config(config: dict[str, Any]) -> tuple[str | None, str | None]:
    for path in (
        ("telegram", "home_channel"),
        ("platforms", "telegram", "home_channel"),
        ("gateway", "platforms", "telegram", "home_channel"),
    ):
        value = _deep_get(config, path)
        if isinstance(value, dict):
            chat_id = value.get("chat_id") or value.get("id")
            thread_id = value.get("thread_id")
            return (
                str(chat_id) if chat_id else None,
                str(thread_id) if thread_id else None,
            )
    return None, None


def _telegram_chat_from_target(
    target: str, config: dict[str, Any]
) -> tuple[str | None, str | None]:
    parsed = parse_notification_target(target)
    chat_id = parsed.chat_id
    thread_id = parsed.thread_id
    if chat_id:
        return chat_id, thread_id

    env_home = os.getenv("TELEGRAM_HOME_CHANNEL")
    if env_home:
        return env_home, os.getenv("TELEGRAM_HOME_CHANNEL_THREAD_ID") or None
    return _telegram_home_from_config(config)


def _load_telegram_rich_config(target: str) -> TelegramRichConfig:
    load_dotenv(bridge_settings.hermes_home() / ".env", override=False)
    config = _read_hermes_config_yaml()
    extra = _merge_telegram_extra(config)
    chat_id, thread_id = _telegram_chat_from_target(target, config)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        for path in (
            ("telegram", "token"),
            ("platforms", "telegram", "token"),
            ("gateway", "platforms", "telegram", "token"),
        ):
            value = _deep_get(config, path)
            if value:
                token = str(value)
                break
    return TelegramRichConfig(
        token=token,
        chat_id=chat_id,
        thread_id=thread_id,
        extra=extra,
    )


def _telegram_api_endpoint(token: str, base_url: str | None = None) -> str:
    if not base_url:
        return f"https://api.telegram.org/bot{token}/sendRichMessage"

    base = base_url.rstrip("/")
    if "{token}" in base:
        return f"{base.format(token=token)}/sendRichMessage"
    if base.endswith(f"/bot{token}"):
        return f"{base}/sendRichMessage"
    if base.endswith("/bot"):
        return f"{base}{token}/sendRichMessage"
    return f"{base}/bot{token}/sendRichMessage"


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return bool(value)


def _rich_messages_enabled(extra: dict[str, Any]) -> bool:
    return _coerce_bool(extra.get("rich_messages"), False)


def _needs_rich_rendering(content: str) -> bool:
    if not content:
        return False
    if any(_TABLE_SEPARATOR_RE.match(line) for line in content.splitlines()):
        return True
    if re.search(r"(?m)^\s*[-*]\s+\[[ xX]\]\s+", content):
        return True
    if re.search(r"(?m)^<details\b|^</details>|^<summary\b|^</summary>", content):
        return True
    return "$$" in content


def _has_details_math_crash_shape(content: str) -> bool:
    if not content:
        return False
    for details_block in _RICH_DETAILS_RE.findall(content):
        if _RICH_MATH_IN_DETAILS_RE.search(details_block):
            return True
    return False


def _content_fits_rich_limits(content: str) -> bool:
    return len(content) <= RICH_MESSAGE_MAX_CHARS


def _rich_normalize_linebreaks(text: str) -> str:
    if not text or "\n" not in text:
        return text

    out: list[str] = []
    pos = 0
    for match in _RICH_PROTECTED_REGION_RE.finditer(text):
        prose = text[pos : match.start()]
        out.append(re.sub(r"(?<!\n)\n(?!\n)", "  \n", prose))
        out.append(match.group(0))
        pos = match.end()
    tail = text[pos:]
    out.append(re.sub(r"(?<!\n)\n(?!\n)", "  \n", tail))
    return "".join(out)


def _rich_message_payload(content: str) -> dict[str, Any]:
    return {"markdown": _rich_normalize_linebreaks(content)}


def _rich_skip_reason(message: str, extra: dict[str, Any]) -> str | None:
    if not _rich_messages_enabled(extra):
        return "telegram rich_messages is disabled"
    if not message or not message.strip():
        return "message is empty"
    if not _needs_rich_rendering(message):
        return "message is not rich-eligible"
    if _has_details_math_crash_shape(message):
        return "message contains details+math content skipped for Telegram Desktop safety"
    if not _content_fits_rich_limits(message):
        return f"message exceeds Telegram rich limit of {RICH_MESSAGE_MAX_CHARS} characters"
    return None


def _telegram_chat_id_value(chat_id: str) -> int | str:
    normalized = str(chat_id).strip()
    if normalized.lstrip("-").isdigit():
        return int(normalized)
    return normalized


def _telegram_proxy_url() -> str | None:
    value = os.getenv("TELEGRAM_PROXY")
    return value.strip() if value and value.strip() else None


async def send_telegram_rich_text(target: str, message: str) -> TelegramRichSendResult:
    parsed = parse_notification_target(target)
    if parsed.platform != "telegram":
        return TelegramRichSendResult(sent=False, reason="target is not telegram")

    rich_config = _load_telegram_rich_config(target)
    if not rich_config.token:
        return TelegramRichSendResult(sent=False, reason="telegram bot token is not configured")
    if not rich_config.chat_id:
        return TelegramRichSendResult(sent=False, reason="telegram chat target is not configured")

    skip_reason = _rich_skip_reason(message, rich_config.extra)
    if skip_reason:
        return TelegramRichSendResult(sent=False, reason=skip_reason)

    payload: dict[str, Any] = {
        "chat_id": _telegram_chat_id_value(rich_config.chat_id),
        "rich_message": _rich_message_payload(message),
    }
    if rich_config.thread_id is not None:
        try:
            payload["message_thread_id"] = int(rich_config.thread_id)
        except ValueError:
            return TelegramRichSendResult(
                sent=False,
                reason=f"invalid telegram thread_id {rich_config.thread_id!r}",
            )

    base_url = rich_config.extra.get("base_url")
    endpoint = _telegram_api_endpoint(rich_config.token, base_url)
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=8.0)
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    proxy_url = _telegram_proxy_url()
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, json=payload)

    if response.status_code >= 400:
        reason = response.text[:1000]
        return TelegramRichSendResult(sent=False, reason=reason, stderr=reason)

    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {}
    if not response_payload.get("ok", False):
        reason = str(response_payload or response.text)[:1000]
        return TelegramRichSendResult(sent=False, reason=reason, stderr=reason)

    result = response_payload.get("result") if isinstance(response_payload, dict) else {}
    message_id = str(result.get("message_id")) if isinstance(result, dict) and result.get("message_id") else None
    LOGGER.info(
        "Sent Telegram rich notification to %s message_id=%s",
        rich_config.chat_id,
        message_id,
    )
    return TelegramRichSendResult(
        sent=True,
        stdout=f"telegram rich sent message_id={message_id or ''}\n",
        message_id=message_id,
    )
