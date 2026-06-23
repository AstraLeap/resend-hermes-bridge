from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import app as bridge_app
from db.state import EventStatus, InboundStatus, OutboundStatus, StepStatus
from utils.email_core import email_address_list


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def open_db() -> sqlite3.Connection:
    bridge_app.SETTINGS.bridge_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(bridge_app.SETTINGS.bridge_db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with open_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                svix_id TEXT PRIMARY KEY,
                email_id TEXT NOT NULL,
                status TEXT NOT NULL,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_email_id ON events(email_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                svix_id TEXT,
                event_type TEXT,
                email_id TEXT,
                received_at TEXT NOT NULL,
                raw_body TEXT,
                verified_json TEXT,
                headers_json TEXT,
                ignored INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_webhook_events_email_id
            ON webhook_events(email_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_webhook_events_svix_id
            ON webhook_events(svix_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_emails (
                email_id TEXT PRIMARY KEY,
                svix_id TEXT,
                event_type TEXT,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                addressed_to_inbound INTEGER,
                from_address TEXT,
                to_addresses_json TEXT,
                cc_addresses_json TEXT,
                bcc_addresses_json TEXT,
                subject TEXT,
                message_id TEXT,
                text_body TEXT,
                html_body TEXT,
                headers_json TEXT,
                raw_event_json TEXT,
                raw_email_json TEXT,
                raw_attachments_json TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT NOT NULL,
                attachment_id TEXT,
                filename TEXT,
                content_type TEXT,
                content_disposition TEXT,
                size INTEGER,
                relevant INTEGER NOT NULL DEFAULT 0,
                download_url TEXT,
                local_path TEXT,
                text_snippet TEXT,
                skipped TEXT,
                error TEXT,
                raw_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(email_id) REFERENCES inbound_emails(email_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_attachments_email_id
            ON attachments(email_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hermes_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                action TEXT,
                prompt_json TEXT,
                response_content TEXT,
                decision_json TEXT,
                error TEXT,
                FOREIGN KEY(email_id) REFERENCES inbound_emails(email_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_hermes_decisions_email_id
            ON hermes_decisions(email_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                recipient TEXT,
                subject TEXT,
                body_text TEXT,
                payload_json TEXT,
                response_json TEXT,
                external_id TEXT,
                stdout TEXT,
                stderr TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbound_messages_email_id
            ON outbound_messages(email_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                svix_id TEXT,
                email_id TEXT,
                step TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                detail_json TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_processing_steps_email_id
            ON processing_steps(email_id)
            """
        )
        ensure_schema_version(conn)
    harden_storage_permissions()


def ensure_schema_version(conn: sqlite3.Connection) -> None:
    row = conn.execute("PRAGMA user_version").fetchone()
    current = int(row[0] if row else 0)
    if current > bridge_app.SCHEMA_VERSION:
        bridge_app.LOGGER.warning(
            "database schema version %s is newer than this app version %s",
            current,
            bridge_app.SCHEMA_VERSION,
        )
        return
    if current < bridge_app.SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {bridge_app.SCHEMA_VERSION}")


def harden_storage_permissions() -> None:
    storage_paths: list[tuple[Path, int]] = [
        (bridge_app.SETTINGS.bridge_db, 0o600),
        (bridge_app.SETTINGS.attachment_dir, 0o700),
        (bridge_app.BOT_REPLY_CONTEXT_DIR, 0o700),
    ]
    for path, mode in storage_paths:
        try:
            if path.exists():
                path.chmod(mode)
        except OSError:
            bridge_app.LOGGER.warning("could not chmod %s", path)


def cleanup_old_history() -> None:
    """Prune old audit rows so the local SQLite database does not grow forever."""
    if bridge_app.SETTINGS.retention_days <= 0:
        return
    cutoff = (
        datetime.now(UTC) - timedelta(days=bridge_app.SETTINGS.retention_days)
    ).isoformat()
    deleted: dict[str, int] = {}
    try:
        with open_db() as conn:
            for table, column in (
                ("attachments", "created_at"),
                ("hermes_decisions", "created_at"),
                ("outbound_messages", "created_at"),
                ("processing_steps", "created_at"),
                ("webhook_events", "received_at"),
                ("events", "received_at"),
                ("inbound_emails", "received_at"),
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)
                )
                deleted[table] = int(cursor.rowcount or 0)
    except sqlite3.IntegrityError as exc:
        bridge_app.LOGGER.warning("retention cleanup skipped due to related rows: %s", exc)
        return

    if any(deleted.values()):
        bridge_app.LOGGER.info("retention cleanup pruned rows older than %s: %s", cutoff, deleted)
        record_processing_step(
            step="retention_cleanup",
            status=StepStatus.DONE,
            detail={"cutoff": cutoff, "deleted": deleted},
        )


def load_recoverable_events() -> list[sqlite3.Row]:
    recoverable = [EventStatus.PENDING]
    if bridge_app.SETTINGS.recover_failed_events:
        recoverable.append(EventStatus.FAILED)
    status_sql = "(" + ", ".join(f"'{status}'" for status in recoverable) + ")"
    completed_inbound_sql = (
        f"('{InboundStatus.REPLIED}', '{InboundStatus.NOTIFIED}')"
    )
    with open_db() as conn:
        return list(
            conn.execute(
                f"""
                SELECT e.svix_id, e.email_id, e.status, w.verified_json
                FROM events e
                JOIN webhook_events w
                  ON w.id = (
                    SELECT MAX(w2.id)
                    FROM webhook_events w2
                    WHERE w2.svix_id = e.svix_id
                      AND w2.event_type = 'email.received'
                  )
                LEFT JOIN inbound_emails i ON i.email_id = e.email_id
                WHERE e.status IN {status_sql}
                  AND (i.status IS NULL OR i.status NOT IN {completed_inbound_sql})
                  AND NOT EXISTS (
                    SELECT 1
                    FROM processing_steps ps
                    WHERE ps.email_id = e.email_id
                      AND ps.step = 'resend_reply'
                      AND ps.status = '{StepStatus.SENT}'
                  )
                ORDER BY e.received_at ASC
                LIMIT ?
                """,
                (bridge_app.SETTINGS.event_recovery_limit,),
            )
        )


def db_health() -> dict[str, Any]:
    try:
        with open_db() as conn:
            conn.execute("SELECT 1").fetchone()
            row = conn.execute("PRAGMA user_version").fetchone()
            schema_version = int(row[0] if row else 0)
        return {"ok": True, "schema_version": schema_version}
    except Exception as exc:
        return {"ok": False, "error": bridge_app.exception_message(exc)}


def record_pending_event(svix_id: str, email_id: str) -> bool:
    try:
        with open_db() as conn:
            conn.execute(
                """
                INSERT INTO events (svix_id, email_id, status, received_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (svix_id, email_id, EventStatus.PENDING, now_iso(), now_iso()),
            )
        return True
    except sqlite3.IntegrityError:
        bridge_app.LOGGER.info(
            "duplicate webhook skipped: svix_id=%s email_id=%s", svix_id, email_id
        )
        return False


def record_webhook_event(
    *,
    svix_id: str,
    event_type: str | None,
    email_id: str | None,
    raw_body: bytes,
    event: dict[str, Any],
    headers_json: str,
    ignored: bool,
) -> None:
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO webhook_events (
                svix_id, event_type, email_id, received_at, raw_body,
                verified_json, headers_json, ignored
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                svix_id,
                event_type,
                email_id,
                now_iso(),
                raw_body.decode("utf-8", errors="replace"),
                json_dumps(event),
                headers_json,
                1 if ignored else 0,
            ),
        )


def record_processing_step(
    *,
    step: str,
    status: str,
    svix_id: str | None = None,
    email_id: str | None = None,
    detail: Any | None = None,
    error: str | None = None,
) -> None:
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO processing_steps (
                svix_id, email_id, step, status, created_at, detail_json, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                svix_id,
                email_id,
                step,
                status,
                now_iso(),
                json_dumps(detail) if detail is not None else None,
                error,
            ),
        )


def record_inbound_email(
    *,
    svix_id: str,
    event: dict[str, Any],
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
    addressed_to_inbound: bool,
    status: str = InboundStatus.FETCHED,
    error: str | None = None,
) -> None:
    event_type = str(event.get("type") or "")
    email_id = str(email.get("id") or (event.get("data") or {}).get("email_id") or "")
    now = now_iso()
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO inbound_emails (
                email_id, svix_id, event_type, received_at, updated_at, status,
                addressed_to_inbound, from_address, to_addresses_json,
                cc_addresses_json, bcc_addresses_json, subject, message_id,
                text_body, html_body, headers_json, raw_event_json,
                raw_email_json, raw_attachments_json, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email_id) DO UPDATE SET
                svix_id = excluded.svix_id,
                event_type = excluded.event_type,
                updated_at = excluded.updated_at,
                status = excluded.status,
                addressed_to_inbound = excluded.addressed_to_inbound,
                from_address = excluded.from_address,
                to_addresses_json = excluded.to_addresses_json,
                cc_addresses_json = excluded.cc_addresses_json,
                bcc_addresses_json = excluded.bcc_addresses_json,
                subject = excluded.subject,
                message_id = excluded.message_id,
                text_body = excluded.text_body,
                html_body = excluded.html_body,
                headers_json = excluded.headers_json,
                raw_event_json = excluded.raw_event_json,
                raw_email_json = excluded.raw_email_json,
                raw_attachments_json = excluded.raw_attachments_json,
                error = excluded.error
            """,
            (
                email_id,
                svix_id,
                event_type,
                now,
                now,
                status,
                1 if addressed_to_inbound else 0,
                str(email.get("from") or ""),
                json_dumps(email_address_list(email, "to")),
                json_dumps(email_address_list(email, "cc")),
                json_dumps(email_address_list(email, "bcc")),
                str(email.get("subject") or ""),
                str(email.get("message_id") or ""),
                str(email.get("text") or ""),
                str(email.get("html") or ""),
                json_dumps(email.get("headers") or {}),
                json_dumps(event),
                json_dumps(email),
                json_dumps(attachments),
                error,
            ),
        )


def update_inbound_status(email_id: str, status: str, error: str | None = None) -> None:
    with open_db() as conn:
        conn.execute(
            """
            UPDATE inbound_emails
            SET status = ?, updated_at = ?, error = ?
            WHERE email_id = ?
            """,
            (status, now_iso(), error, email_id),
        )


def record_attachment_history(
    *,
    email_id: str,
    raw_attachment: dict[str, Any],
    item: dict[str, Any],
) -> None:
    now = now_iso()
    attachment_id = str(raw_attachment.get("id") or item.get("id") or "")
    filename = str(item.get("filename") or raw_attachment.get("filename") or "")
    content_type = str(
        item.get("content_type") or raw_attachment.get("content_type") or ""
    )
    content_disposition = str(raw_attachment.get("content_disposition") or "")
    size = int(item.get("size") or raw_attachment.get("size") or 0)
    relevant = 1 if item.get("relevant") else 0
    download_url = str(raw_attachment.get("download_url") or "")
    local_path = str(item.get("local_path") or "")
    text_snippet = str(item.get("text_snippet") or "")
    skipped = str(item.get("skipped") or "")
    error = str(item.get("error") or "")
    raw_json = json_dumps(raw_attachment)
    with open_db() as conn:
        row: sqlite3.Row | None = None
        if attachment_id:
            row = conn.execute(
                """
                SELECT id FROM attachments
                WHERE email_id = ? AND attachment_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (email_id, attachment_id),
            ).fetchone()
        if row is None and filename:
            row = conn.execute(
                """
                SELECT id FROM attachments
                WHERE email_id = ? AND filename = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (email_id, filename),
            ).fetchone()
        if row is not None:
            conn.execute(
                """
                UPDATE attachments
                SET attachment_id = ?, filename = ?, content_type = ?,
                    content_disposition = ?, size = ?, relevant = ?,
                    download_url = ?, local_path = ?, text_snippet = ?,
                    skipped = ?, error = ?, raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    attachment_id,
                    filename,
                    content_type,
                    content_disposition,
                    size,
                    relevant,
                    download_url,
                    local_path,
                    text_snippet,
                    skipped,
                    error,
                    raw_json,
                    now,
                    int(row["id"]),
                ),
            )
            return
        conn.execute(
            """
            INSERT INTO attachments (
                email_id, attachment_id, filename, content_type,
                content_disposition, size, relevant, download_url, local_path,
                text_snippet, skipped, error, raw_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                attachment_id,
                filename,
                content_type,
                content_disposition,
                size,
                relevant,
                download_url,
                local_path,
                text_snippet,
                skipped,
                error,
                raw_json,
                now,
                now,
            ),
        )


def record_hermes_decision(
    *,
    email_id: str,
    prompt: dict[str, Any],
    response_content: str | None = None,
    decision: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO hermes_decisions (
                email_id, created_at, action, prompt_json, response_content,
                decision_json, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                now_iso(),
                str((decision or {}).get("action") or ""),
                json_dumps(prompt),
                response_content,
                json_dumps(decision) if decision is not None else None,
                error,
            ),
        )


def create_outbound_message(
    *,
    kind: str,
    email_id: str | None,
    recipient: str,
    subject: str | None = None,
    body_text: str | None = None,
    payload: Any | None = None,
) -> int:
    now = now_iso()
    with open_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO outbound_messages (
                email_id, kind, status, created_at, updated_at, recipient,
                subject, body_text, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                kind,
                OutboundStatus.PENDING,
                now,
                now,
                recipient,
                subject,
                body_text,
                json_dumps(payload) if payload is not None else None,
            ),
        )
        return int(cursor.lastrowid)


def update_outbound_message(
    outbound_id: int,
    *,
    status: str,
    response: Any | None = None,
    external_id: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    error: str | None = None,
) -> None:
    with open_db() as conn:
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = ?, updated_at = ?, response_json = ?, external_id = ?,
                stdout = ?, stderr = ?, error = ?
            WHERE id = ?
            """,
            (
                status,
                now_iso(),
                json_dumps(response) if response is not None else None,
                external_id,
                stdout,
                stderr,
                error,
                outbound_id,
            ),
        )


def mark_event_done(svix_id: str) -> None:
    with open_db() as conn:
        conn.execute(
            "UPDATE events SET status = ?, updated_at = ?, error = NULL WHERE svix_id = ?",
            (EventStatus.DONE, now_iso(), svix_id),
        )


def mark_event_failed(svix_id: str, exc: Exception) -> None:
    with open_db() as conn:
        conn.execute(
            "UPDATE events SET status = ?, updated_at = ?, error = ? WHERE svix_id = ?",
            (EventStatus.FAILED, now_iso(), str(exc)[:1000], svix_id),
        )
