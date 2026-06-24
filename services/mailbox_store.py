from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from settings import APP_DIR

STATE_DB_FILE = APP_DIR / "data" / "state.db"
VALID_MESSAGE_KINDS = {"inbound", "outbound"}
MAX_LABEL_LENGTH = 64
MAX_SEARCH_LIMIT = 100
DEFAULT_BODY_LIMIT = 12000
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class MailboxStoreError(ValueError):
    pass


class MailboxNotFoundError(MailboxStoreError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def open_mailbox_db(db_path: Path = STATE_DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def normalize_message_kind(kind: Any) -> str:
    normalized = str(kind or "inbound").strip().lower()
    aliases = {
        "received": "inbound",
        "receive": "inbound",
        "incoming": "inbound",
        "sent": "outbound",
        "send": "outbound",
        "outgoing": "outbound",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_MESSAGE_KINDS:
        raise MailboxStoreError("kind must be 'inbound' or 'outbound'")
    return normalized


def normalize_label(label: Any) -> str:
    normalized = str(label or "").strip()
    if not normalized:
        raise MailboxStoreError("label must not be empty")
    if len(normalized) > MAX_LABEL_LENGTH:
        raise MailboxStoreError(f"label must be at most {MAX_LABEL_LENGTH} characters")
    if CONTROL_CHAR_RE.search(normalized):
        raise MailboxStoreError("label must not contain control characters")
    return normalized


def normalize_labels(labels: Any) -> list[str]:
    if labels in (None, ""):
        return []
    if isinstance(labels, str):
        raw_items = [item.strip() for item in labels.split(",")]
    elif isinstance(labels, (list, tuple, set)):
        raw_items = list(labels)
    else:
        raw_items = [labels]

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if item in (None, ""):
            continue
        label = normalize_label(item)
        key = label.casefold()
        if key not in seen:
            normalized.append(label)
            seen.add(key)
    return normalized


def search_mailbox(
    *,
    db_path: Path = STATE_DB_FILE,
    query: str = "",
    label: str = "",
    direction: str = "all",
    status: str = "",
    limit: int = 20,
    offset: int = 0,
    include_deleted: bool = False,
) -> dict[str, Any]:
    direction = str(direction or "all").strip().lower()
    if direction in {"received", "incoming"}:
        direction = "inbound"
    elif direction in {"sent", "outgoing"}:
        direction = "outbound"
    if direction not in {"all", "inbound", "outbound"}:
        raise MailboxStoreError("direction must be 'all', 'inbound', or 'outbound'")

    limit = max(1, min(int(limit or 20), MAX_SEARCH_LIMIT))
    offset = max(0, int(offset or 0))
    query_text = str(query or "").strip().casefold()
    status_text = str(status or "").strip().casefold()
    label_text = str(label or "").strip().casefold()

    if not db_path.exists():
        return {
            "items": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "database": str(db_path),
        }

    with open_mailbox_db(db_path) as conn:
        rows: list[dict[str, Any]] = []
        if direction in {"all", "inbound"} and _table_exists(conn, "inbound_emails"):
            rows.extend(_fetch_inbound_summaries(conn, include_deleted=include_deleted))
        if direction in {"all", "outbound"} and _table_exists(conn, "outbound_messages"):
            rows.extend(_fetch_outbound_summaries(conn, include_deleted=include_deleted))

        for row in rows:
            row["labels"] = get_labels(
                row["kind"],
                row["message_id"],
                conn=conn,
            )

    filtered = [
        row
        for row in rows
        if _matches_summary(row, query_text=query_text, status_text=status_text)
        and _matches_label(row, label_text=label_text)
    ]
    filtered.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    page = filtered[offset : offset + limit]
    return {
        "items": page,
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "database": str(db_path),
    }


def get_mailbox_email(
    *,
    kind: str,
    message_id: str,
    db_path: Path = STATE_DB_FILE,
    body_limit: int = DEFAULT_BODY_LIMIT,
) -> dict[str, Any]:
    kind = normalize_message_kind(kind)
    message_id = str(message_id or "").strip()
    if not message_id:
        raise MailboxStoreError("message_id is required")
    if not db_path.exists():
        raise MailboxNotFoundError(f"{kind} email not found: {message_id}")

    body_limit = max(0, int(body_limit or DEFAULT_BODY_LIMIT))
    with open_mailbox_db(db_path) as conn:
        if kind == "inbound":
            row = conn.execute(
                "SELECT * FROM inbound_emails WHERE email_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise MailboxNotFoundError(f"inbound email not found: {message_id}")
            return _inbound_detail(conn, row, body_limit=body_limit)

        row = conn.execute(
            "SELECT * FROM outbound_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise MailboxNotFoundError(f"outbound email not found: {message_id}")
        return _outbound_detail(conn, row, body_limit=body_limit)


def delete_mailbox_email(
    *,
    kind: str,
    message_id: str,
    reason: str = "",
    restore: bool = False,
    db_path: Path = STATE_DB_FILE,
) -> dict[str, Any]:
    kind = normalize_message_kind(kind)
    message_id = str(message_id or "").strip()
    if not message_id:
        raise MailboxStoreError("message_id is required")
    if not db_path.exists():
        raise MailboxNotFoundError(f"{kind} email not found: {message_id}")

    with open_mailbox_db(db_path) as conn:
        _require_message_exists(conn, kind, message_id)
        if kind == "inbound":
            conn.execute(
                """
                UPDATE inbound_emails
                SET deleted_at = ?, deleted_reason = ?, updated_at = ?
                WHERE email_id = ?
                """,
                (
                    None if restore else now_iso(),
                    None if restore else str(reason or "").strip()[:500],
                    now_iso(),
                    message_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE outbound_messages
                SET deleted_at = ?, deleted_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    None if restore else now_iso(),
                    None if restore else str(reason or "").strip()[:500],
                    now_iso(),
                    message_id,
                ),
            )
    return get_mailbox_email(kind=kind, message_id=message_id, db_path=db_path, body_limit=0)


def update_mailbox_labels(
    *,
    kind: str,
    message_id: str,
    add_labels: Any = None,
    remove_labels: Any = None,
    set_labels: Any = None,
    db_path: Path = STATE_DB_FILE,
) -> dict[str, Any]:
    kind = normalize_message_kind(kind)
    message_id = str(message_id or "").strip()
    if not message_id:
        raise MailboxStoreError("message_id is required")
    if not db_path.exists():
        raise MailboxNotFoundError(f"{kind} email not found: {message_id}")

    add = normalize_labels(add_labels)
    remove = normalize_labels(remove_labels)
    replacement = None if set_labels is None else normalize_labels(set_labels)
    if replacement is None and not add and not remove:
        raise MailboxStoreError("provide add_labels, remove_labels, or set_labels")

    with open_mailbox_db(db_path) as conn:
        _require_message_exists(conn, kind, message_id)
        if replacement is not None:
            conn.execute(
                "DELETE FROM email_labels WHERE message_kind = ? AND message_id = ?",
                (kind, message_id),
            )
            add = replacement
        for label in add:
            conn.execute(
                """
                INSERT OR IGNORE INTO email_labels (
                    message_kind, message_id, label, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (kind, message_id, label, now_iso()),
            )
        for label in remove:
            conn.execute(
                """
                DELETE FROM email_labels
                WHERE message_kind = ? AND message_id = ? AND lower(label) = lower(?)
                """,
                (kind, message_id, label),
            )
        labels = get_labels(kind, message_id, conn=conn)
    return {"kind": kind, "message_id": message_id, "labels": labels}


def list_mailbox_labels(
    *,
    db_path: Path = STATE_DB_FILE,
    query: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 100), MAX_SEARCH_LIMIT))
    query_text = str(query or "").strip().casefold()
    if not db_path.exists():
        return {"labels": [], "total": 0, "limit": limit, "database": str(db_path)}
    with open_mailbox_db(db_path) as conn:
        rows = list(
            conn.execute(
                """
                SELECT label, COUNT(*) AS count
                FROM email_labels
                GROUP BY label
                ORDER BY lower(label) ASC
                """
            )
        )
    labels = [
        {"label": str(row["label"]), "count": int(row["count"])}
        for row in rows
        if not query_text or query_text in str(row["label"]).casefold()
    ]
    return {
        "labels": labels[:limit],
        "total": len(labels),
        "limit": limit,
        "database": str(db_path),
    }


def get_labels(
    kind: str,
    message_id: str,
    *,
    conn: sqlite3.Connection,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT label
        FROM email_labels
        WHERE message_kind = ? AND message_id = ?
        ORDER BY lower(label) ASC
        """,
        (kind, message_id),
    )
    return [str(row["label"]) for row in rows]


def _fetch_inbound_summaries(
    conn: sqlite3.Connection, *, include_deleted: bool
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT email_id, received_at, updated_at, status, addressed_to_inbound,
               from_address, to_addresses_json, cc_addresses_json,
               bcc_addresses_json, subject, text_body, html_body,
               deleted_at, deleted_reason
        FROM inbound_emails
        ORDER BY received_at DESC
        """
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        if row["deleted_at"] and not include_deleted:
            continue
        result.append(_inbound_summary(row))
    return result


def _fetch_outbound_summaries(
    conn: sqlite3.Connection, *, include_deleted: bool
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, email_id, kind, status, created_at, updated_at, recipient,
               subject, body_text, external_id, deleted_at, deleted_reason
        FROM outbound_messages
        ORDER BY created_at DESC
        """
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        if row["deleted_at"] and not include_deleted:
            continue
        result.append(_outbound_summary(row))
    return result


def _inbound_summary(row: sqlite3.Row) -> dict[str, Any]:
    to_addresses = _json_list(row["to_addresses_json"])
    text = str(row["text_body"] or "")
    html = str(row["html_body"] or "")
    return {
        "kind": "inbound",
        "message_id": str(row["email_id"] or ""),
        "timestamp": str(row["received_at"] or ""),
        "received_at": row["received_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "from": row["from_address"],
        "to": to_addresses,
        "subject": row["subject"],
        "preview": _preview(text or html),
        "addressed_to_inbound": bool(row["addressed_to_inbound"]),
        "deleted": bool(row["deleted_at"]),
        "deleted_at": row["deleted_at"],
        "deleted_reason": row["deleted_reason"],
        "_search_text": " ".join(
            [
                str(row["email_id"] or ""),
                str(row["from_address"] or ""),
                " ".join(to_addresses),
                " ".join(_json_list(row["cc_addresses_json"])),
                " ".join(_json_list(row["bcc_addresses_json"])),
                str(row["subject"] or ""),
                text,
                html,
            ]
        ),
    }


def _outbound_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": "outbound",
        "message_id": str(row["id"]),
        "timestamp": str(row["created_at"] or ""),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "delivery_kind": row["kind"],
        "email_id": row["email_id"],
        "recipient": row["recipient"],
        "subject": row["subject"],
        "preview": _preview(str(row["body_text"] or "")),
        "external_id": row["external_id"],
        "deleted": bool(row["deleted_at"]),
        "deleted_at": row["deleted_at"],
        "deleted_reason": row["deleted_reason"],
        "_search_text": " ".join(
            [
                str(row["id"] or ""),
                str(row["email_id"] or ""),
                str(row["recipient"] or ""),
                str(row["subject"] or ""),
                str(row["body_text"] or ""),
                str(row["external_id"] or ""),
            ]
        ),
    }


def _inbound_detail(
    conn: sqlite3.Connection, row: sqlite3.Row, *, body_limit: int
) -> dict[str, Any]:
    message_id = str(row["email_id"] or "")
    return {
        "kind": "inbound",
        "message_id": message_id,
        "received_at": row["received_at"],
        "updated_at": row["updated_at"],
        "status": row["status"],
        "addressed_to_inbound": bool(row["addressed_to_inbound"]),
        "from": row["from_address"],
        "to": _json_list(row["to_addresses_json"]),
        "cc": _json_list(row["cc_addresses_json"]),
        "bcc": _json_list(row["bcc_addresses_json"]),
        "subject": row["subject"],
        "message_id_header": row["message_id"],
        "text_body": _limit_text(row["text_body"], body_limit),
        "html_body": _limit_text(row["html_body"], body_limit),
        "headers": _json_value(row["headers_json"], {}),
        "attachments": _attachments(conn, message_id),
        "labels": get_labels("inbound", message_id, conn=conn),
        "deleted": bool(row["deleted_at"]),
        "deleted_at": row["deleted_at"],
        "deleted_reason": row["deleted_reason"],
        "error": row["error"],
    }


def _outbound_detail(
    conn: sqlite3.Connection, row: sqlite3.Row, *, body_limit: int
) -> dict[str, Any]:
    message_id = str(row["id"])
    return {
        "kind": "outbound",
        "message_id": message_id,
        "email_id": row["email_id"],
        "delivery_kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "recipient": row["recipient"],
        "subject": row["subject"],
        "body_text": _limit_text(row["body_text"], body_limit),
        "payload": _json_value(row["payload_json"], {}),
        "response": _json_value(row["response_json"], {}),
        "external_id": row["external_id"],
        "labels": get_labels("outbound", message_id, conn=conn),
        "deleted": bool(row["deleted_at"]),
        "deleted_at": row["deleted_at"],
        "deleted_reason": row["deleted_reason"],
        "error": row["error"],
    }


def _require_message_exists(
    conn: sqlite3.Connection,
    kind: str,
    message_id: str,
) -> None:
    if kind == "inbound":
        row = conn.execute(
            "SELECT 1 FROM inbound_emails WHERE email_id = ?",
            (message_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM outbound_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    if row is None:
        raise MailboxNotFoundError(f"{kind} email not found: {message_id}")


def _attachments(conn: sqlite3.Connection, email_id: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "attachments"):
        return []
    rows = conn.execute(
        """
        SELECT attachment_id, filename, content_type, content_disposition,
               size, relevant, local_path, text_snippet, skipped, error
        FROM attachments
        WHERE email_id = ?
        ORDER BY id ASC
        """,
        (email_id,),
    )
    return [
        {
            "attachment_id": row["attachment_id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "content_disposition": row["content_disposition"],
            "size": row["size"],
            "relevant": bool(row["relevant"]),
            "local_path": row["local_path"],
            "text_snippet": row["text_snippet"],
            "skipped": row["skipped"],
            "error": row["error"],
        }
        for row in rows
    ]


def _matches_summary(
    row: dict[str, Any],
    *,
    query_text: str,
    status_text: str,
) -> bool:
    if status_text and status_text != str(row.get("status") or "").casefold():
        return False
    if not query_text:
        return True
    return query_text in str(row.get("_search_text") or "").casefold()


def _matches_label(row: dict[str, Any], *, label_text: str) -> bool:
    if not label_text:
        return True
    return any(label_text == str(label).casefold() for label in row.get("labels") or [])


def _json_list(value: Any) -> list[str]:
    parsed = _json_value(value, [])
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _json_value(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return default


def _preview(value: str, limit: int = 300) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def _limit_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"
