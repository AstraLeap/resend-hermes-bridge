from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import settings as bridge_settings

LOGGER = logging.getLogger("resend-hermes-bridge.hermes_context")
RECENT_DUPLICATE_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class HermesContextAppendResult:
    recorded: bool
    reason: str | None = None
    session_id: str | None = None
    message_id: int | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"recorded": self.recorded}
        if self.reason:
            payload["reason"] = self.reason
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.message_id is not None:
            payload["message_id"] = self.message_id
        return payload


@dataclass(frozen=True)
class HermesNotificationTarget:
    platform: str
    chat_id: str | None = None
    thread_id: str | None = None
    chat_type: str | None = None


def parse_notification_target(target: str) -> HermesNotificationTarget:
    parts = [part for part in target.split(":") if part]
    platform = parts[0] if parts else target
    chat_type = None
    chat_id = None
    thread_id = None

    if len(parts) >= 3 and parts[1] in {"dm", "group", "channel", "private"}:
        chat_type = parts[1]
        chat_id = parts[2]
        if len(parts) >= 4:
            thread_id = parts[3]
    elif len(parts) >= 2:
        chat_id = parts[1]
        if len(parts) >= 3:
            thread_id = parts[2]

    return HermesNotificationTarget(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        chat_type=chat_type,
    )


def _sessions_file() -> Path:
    return bridge_settings.hermes_home() / "sessions" / "sessions.json"


def _state_db_file() -> Path:
    return bridge_settings.hermes_home() / "state.db"


def _hermes_agent_dir() -> Path:
    return bridge_settings.hermes_home() / "hermes-agent"


def _load_session_entries(path: Path | None = None) -> list[dict[str, Any]]:
    sessions_path = path or _sessions_file()
    if not sessions_path.exists():
        return []
    raw = json.loads(sessions_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        entries = []
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            entry = dict(value)
            entry.setdefault("session_key", key)
            entries.append(entry)
        return entries
    if isinstance(raw, list):
        return [dict(value) for value in raw if isinstance(value, dict)]
    return []


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _entry_origin(entry: dict[str, Any]) -> dict[str, Any]:
    origin = entry.get("origin")
    return origin if isinstance(origin, dict) else {}


def _entry_matches_target(
    entry: dict[str, Any], target: HermesNotificationTarget
) -> bool:
    origin = _entry_origin(entry)
    platform = _text(entry.get("platform") or origin.get("platform"))
    if platform != target.platform:
        return False

    if target.chat_type:
        chat_type = _text(entry.get("chat_type") or origin.get("chat_type"))
        if chat_type != target.chat_type:
            return False

    if target.chat_id:
        chat_id = _text(
            origin.get("chat_id")
            or origin.get("user_id")
            or entry.get("chat_id")
            or entry.get("user_id")
        )
        if chat_id != target.chat_id:
            return False

    if target.thread_id:
        thread_id = _text(origin.get("thread_id") or entry.get("thread_id"))
        if thread_id != target.thread_id:
            return False

    return bool(entry.get("session_id"))


def _entry_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    updated_at = _text(entry.get("updated_at") or entry.get("created_at")) or ""
    session_id = _text(entry.get("session_id")) or ""
    return updated_at, session_id


def resolve_session_id_for_target(target: str) -> str | None:
    parsed = parse_notification_target(target)
    entries = _load_session_entries()
    matches = [entry for entry in entries if _entry_matches_target(entry, parsed)]
    active_matches = [entry for entry in matches if not entry.get("suspended")]
    candidates = active_matches or matches
    if not candidates:
        return None
    return _text(max(candidates, key=_entry_sort_key).get("session_id"))


def _session_db_class():
    agent_dir = _hermes_agent_dir()
    if not agent_dir.exists():
        raise RuntimeError(f"Hermes agent source not found: {agent_dir}")
    agent_dir_text = str(agent_dir)
    if agent_dir_text not in sys.path:
        sys.path.insert(0, agent_dir_text)
    from hermes_state import SessionDB

    return SessionDB


def _already_in_recent_context(db: Any, session_id: str, message: str) -> bool:
    try:
        recent_messages = db.get_messages(session_id)[-20:]
    except Exception as exc:
        LOGGER.debug("Could not inspect recent Hermes context for dedupe: %s", exc)
        return False

    now = time.time()
    for existing in reversed(recent_messages):
        if existing.get("role") != "assistant":
            continue
        if existing.get("content") != message:
            continue
        timestamp = existing.get("timestamp")
        if timestamp is None:
            return True
        try:
            age = now - float(timestamp)
        except (TypeError, ValueError):
            return True
        if age <= RECENT_DUPLICATE_WINDOW_SECONDS:
            return True
    return False


def append_notification_to_user_context(
    target: str, message: str
) -> HermesContextAppendResult:
    session_id = resolve_session_id_for_target(target)
    if not session_id:
        return HermesContextAppendResult(
            recorded=False,
            reason=f"no active Hermes session found for target {target!r}",
        )

    state_db = _state_db_file()
    if not state_db.exists():
        return HermesContextAppendResult(
            recorded=False,
            reason=f"Hermes state database not found: {state_db}",
            session_id=session_id,
        )

    SessionDB = _session_db_class()
    db = SessionDB(db_path=state_db)
    try:
        if _already_in_recent_context(db, session_id, message):
            return HermesContextAppendResult(
                recorded=False,
                reason="message already exists in recent Hermes context",
                session_id=session_id,
            )

        message_id = db.append_message(
            session_id=session_id,
            role="assistant",
            content=message,
            observed=False,
            timestamp=time.time(),
        )
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()
    LOGGER.info(
        "Recorded %s notification in Hermes session %s as message %s",
        target,
        session_id,
        message_id,
    )
    return HermesContextAppendResult(
        recorded=True,
        session_id=session_id,
        message_id=message_id,
    )
