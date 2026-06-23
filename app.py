from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import hmac
import json
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from pydantic import ValidationError
from svix.webhooks import Webhook

import bridge_settings
import clients.resend_client as resend_client
from bridge_settings import APP_DIR, Settings
from db.state import EventStatus, InboundStatus, OutboundStatus, StepStatus
from models.send_models import SendRequest
from services.hermes_client import (  # noqa: F401
    build_hermes_api_messages,
    build_hermes_task_prompt,
    coerce_bool,
    decode_common_json_escapes,
    fallback_notify_decision,
    image_path_to_data_url,
    parse_json_decision,
    parse_loose_decision_object,
    parse_loose_string_value,
    run_hermes_api_server_task,
    run_hermes_email_task,
    strip_json_code_fence,
)
from services.notification import notify_telegram
from utils.email_core import (
    EmailValidationError,
    clean_header_value,
    email_address_list,
    ensure_list,
    outbound_recipient_summary,
    parse_email_addresses,
)
from utils.email_core import (
    resolve_sender as resolve_email_sender,
)
from utils.notices import render_inbound_email_notice, render_processing_result_notice

bridge_settings.load_project_env()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("resend-hermes-bridge")
NOTIFICATION_BOT_TITLE = "{AI_NAME}收到邮件啦！正在处理中哦~"
SCHEMA_VERSION = 1


def exception_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__


def _require_env(name: str) -> str:
    return bridge_settings.require_env(name)


def _require_secret_env(name: str) -> str:
    return bridge_settings.require_secret_env(name)


def _hermes_send_bin() -> Path:
    return bridge_settings.hermes_send_bin()


def _hermes_home() -> Path:
    return bridge_settings.hermes_home()


def _strip_simple_yaml_value(value: str) -> str:
    return bridge_settings.strip_simple_yaml_value(value)


def _read_hermes_config() -> dict[str, str]:
    return bridge_settings.read_hermes_config()


def _require_hermes_config(config: dict[str, str], name: str) -> str:
    return bridge_settings.require_hermes_config(config, name)


def _api_server_enabled(config: dict[str, str]) -> bool:
    return bridge_settings.api_server_enabled(config)


def _hermes_api_url() -> str:
    return bridge_settings.hermes_api_url()


def _hermes_api_key() -> str:
    return bridge_settings.hermes_api_key()


SETTINGS = bridge_settings.load_settings()
USER_AGENT = SETTINGS.user_agent
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log"}
RELEVANT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".json",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".xml",
    ".yaml",
    ".yml",
    ".zip",
}
BOT_REPLY_CONTEXT_DIR = SETTINGS.bot_reply_context_dir
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
MAX_OUTBOUND_ATTACHMENTS = 20
GENERATED_ATTACHMENT_ROOTS = SETTINGS.generated_attachment_roots

@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    global SETTINGS, USER_AGENT, BOT_REPLY_CONTEXT_DIR, GENERATED_ATTACHMENT_ROOTS
    if settings is not None:
        SETTINGS = settings
        USER_AGENT = settings.user_agent
        BOT_REPLY_CONTEXT_DIR = settings.bot_reply_context_dir
        GENERATED_ATTACHMENT_ROOTS = settings.generated_attachment_roots
    from routers import health

    fast_app = FastAPI(title="Resend Hermes Bridge", lifespan=lifespan)
    fast_app.include_router(health.router)
    return fast_app


app = create_app()
RECOVERY_TASKS: set[asyncio.Task] = set()


def bot_sender_address() -> str:
    return SETTINGS.inbound_address


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def open_db() -> sqlite3.Connection:
    SETTINGS.bridge_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SETTINGS.bridge_db, timeout=30)
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
    if current > SCHEMA_VERSION:
        LOGGER.warning(
            "database schema version %s is newer than this app version %s",
            current,
            SCHEMA_VERSION,
        )
        return
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def harden_storage_permissions() -> None:
    storage_paths: list[tuple[Path, int]] = [
        (SETTINGS.bridge_db, 0o600),
        (SETTINGS.attachment_dir, 0o700),
        (BOT_REPLY_CONTEXT_DIR, 0o700),
    ]
    for path, mode in storage_paths:
        try:
            if path.exists():
                path.chmod(mode)
        except OSError:
            LOGGER.warning("could not chmod %s", path)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_mcp_draft(draft_id: str) -> dict[str, Any] | None:
    if not SETTINGS.mcp_drafts_file.exists():
        return None
    SETTINGS.mcp_drafts_lock_file.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS.mcp_drafts_lock_file.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        try:
            data = json.loads(SETTINGS.mcp_drafts_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    if not isinstance(data, dict):
        return None
    draft = data.get(draft_id)
    return draft if isinstance(draft, dict) else None


def _draft_payload_subset(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "from_email",
        "from_local",
        "from_name",
        "to",
        "cc",
        "bcc",
        "reply_to",
        "subject",
        "text",
        "html",
        "attachments",
    ):
        value = raw.get(key)
        if value not in (None, "", []):
            result[key] = value
    return result


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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
    if len(items) > MAX_OUTBOUND_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"attachments has too many items; max {MAX_OUTBOUND_ATTACHMENTS}",
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
        inbound_root = (SETTINGS.attachment_dir / safe_filename(email_id)).resolve()

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
                path = APP_DIR / path
            path = path.resolve()
            if inbound_root is not None:
                allowed_roots = [inbound_root] + GENERATED_ATTACHMENT_ROOTS
                if not any(_path_is_relative_to(path, root) for root in allowed_roots):
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
            if file_size > SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}] is larger than {SETTINGS.max_outbound_attachment_bytes} bytes",
                )
            total_bytes += file_size
            if total_bytes > SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments total is larger than {SETTINGS.max_outbound_attachment_bytes} bytes",
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
            if file_size > SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments[{index}] is larger than {SETTINGS.max_outbound_attachment_bytes} bytes",
                )
            total_bytes += file_size
            if total_bytes > SETTINGS.max_outbound_attachment_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=f"attachments total is larger than {SETTINGS.max_outbound_attachment_bytes} bytes",
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


def _validate_agent_attachment_paths(paths: list[str]) -> list[str]:
    allowed_roots = [SETTINGS.attachment_dir.resolve()] + GENERATED_ATTACHMENT_ROOTS
    valid: list[str] = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = APP_DIR / path
        path = path.resolve()
        if not path.is_file():
            LOGGER.warning("owner_report_attachment does not exist: %s", raw)
            continue
        if not any(_path_is_relative_to(path, root) for root in allowed_roots):
            LOGGER.warning(
                "owner_report_attachment outside allowed directories: %s", raw
            )
            continue
        valid.append(str(path))
    return valid


def _require_mcp_draft_confirmation(raw: dict[str, Any]) -> None:
    try:
        draft_id = clean_header_value(raw.get("draft_id"), "draft_id", limit=64)
        approval_token = clean_header_value(
            raw.get("approval_token"), "approval_token", limit=128
        )
    except EmailValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not draft_id or not approval_token:
        raise HTTPException(
            status_code=400,
            detail="draft_id and approval_token are required to send manual email",
        )
    draft = _read_mcp_draft(draft_id)
    if not draft:
        raise HTTPException(
            status_code=400, detail=f"unknown or expired draft_id: {draft_id}"
        )
    if str(draft.get("approval_token") or "").strip() != approval_token:
        raise HTTPException(
            status_code=400, detail=f"invalid approval token for draft_id: {draft_id}"
        )
    if draft.get("sent"):
        raise HTTPException(
            status_code=400, detail=f"draft_id already sent: {draft_id}"
        )
    draft_payload = draft.get("payload")
    if not isinstance(draft_payload, dict):
        raise HTTPException(
            status_code=400, detail=f"invalid draft payload for draft_id: {draft_id}"
        )
    if json_dumps(_draft_payload_subset(raw)) != json_dumps(
        _draft_payload_subset(draft_payload)
    ):
        raise HTTPException(
            status_code=400, detail=f"send payload does not match draft_id: {draft_id}"
        )


def request_headers_json(request: Request) -> str:
    return json_dumps({key: value for key, value in request.headers.items()})


def resolve_sender(raw: dict[str, Any]) -> str:
    try:
        return resolve_email_sender(
            raw,
            domain=SETTINGS.resend_domain,
            default_from=SETTINGS.owner_address,
        )
    except EmailValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bot_reply_context_path(email_id: str) -> Path:
    return BOT_REPLY_CONTEXT_DIR / f"{safe_filename(email_id)}.json"


def _write_bot_reply_context(email_id: str, *, sender: str, reply_to: str) -> None:
    BOT_REPLY_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    context = {
        "email_id": email_id,
        "sender": sender,
        "reply_to": reply_to,
        "created_at": now_iso(),
    }
    path = _bot_reply_context_path(email_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def _read_bot_reply_context(email_id: str) -> dict[str, Any] | None:
    path = _bot_reply_context_path(email_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _delete_bot_reply_context(email_id: str) -> None:
    path = _bot_reply_context_path(email_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("could not remove bot reply context for %s", email_id)


def _is_authorized_bot_reply(raw: dict[str, Any]) -> bool:
    email_id = str(raw.get("auto_reply_email_id") or "").strip()
    if not email_id:
        return False
    context = _read_bot_reply_context(email_id)
    if not context:
        return False
    sender = parseaddr(str(context.get("sender") or ""))[1] or str(
        context.get("sender") or ""
    )
    if sender.lower() != bot_sender_address():
        return False
    expected_to = parseaddr(str(context.get("reply_to") or ""))[1] or str(
        context.get("reply_to") or ""
    )
    if not expected_to:
        return False
    raw_to = [
        parseaddr(str(item))[1] or str(item) for item in ensure_list(raw.get("to"))
    ]
    if len(raw_to) != 1 or raw_to[0].lower() != expected_to.lower():
        return False
    return True


def normalize_send_payload(
    raw: Any, *, allow_bot_sender: bool = False
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="JSON object body is required")
    try:
        raw = SendRequest.model_validate(raw).raw_payload()
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc
    authorized_bot_reply = allow_bot_sender or _is_authorized_bot_reply(raw)
    if raw.get("confirmed") is not True and not authorized_bot_reply:
        raise HTTPException(
            status_code=400, detail="confirmed=true is required to send email"
        )
    if not authorized_bot_reply:
        _require_mcp_draft_confirmation(raw)

    try:
        payload: dict[str, Any] = {
            "from": resolve_sender(raw),
            "to": parse_email_addresses(raw.get("to"), "to", required=True),
            "subject": clean_header_value(raw.get("subject"), "subject", required=True),
        }

        cc = parse_email_addresses(raw.get("cc"), "cc")
        bcc = parse_email_addresses(raw.get("bcc"), "bcc")
        reply_to = parse_email_addresses(raw.get("reply_to"), "reply_to")
    except EmailValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if authorized_bot_reply:
        parsed_sender = parseaddr(str(payload["from"] or ""))[1] or str(
            payload["from"] or ""
        )
        if parsed_sender.lower() != bot_sender_address():
            raise HTTPException(
                status_code=400,
                detail=f"authorized bot auto-replies must send as {bot_sender_address()}",
            )
    if cc:
        payload["cc"] = cc
    if bcc:
        payload["bcc"] = bcc
    if reply_to:
        payload["reply_to"] = reply_to

    raw_headers = raw.get("headers")
    if raw_headers:
        if not isinstance(raw_headers, dict):
            raise HTTPException(status_code=400, detail="headers must be an object")
        headers: dict[str, str] = {}
        for key, value in raw_headers.items():
            header_name = clean_header_value(
                key, "headers key", required=True, limit=100
            )
            if not HEADER_NAME_RE.match(header_name):
                raise HTTPException(
                    status_code=400, detail=f"invalid header name: {header_name}"
                )
            header_value = clean_header_value(
                value, f"headers[{header_name}]", required=True, limit=998
            )
            headers[header_name] = header_value
        if headers:
            payload["headers"] = headers

    text = str(raw.get("text") or "").strip()
    html = str(raw.get("html") or "").strip()
    if not text and not html:
        raise HTTPException(status_code=400, detail="text or html body is required")
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html
    attachments = normalize_outbound_attachments(
        raw.get("attachments"),
        email_id=str(raw.get("auto_reply_email_id") or raw.get("email_id") or ""),
        restrict_to_inbound=authorized_bot_reply,
    )
    if attachments:
        payload["attachments"] = attachments
    return payload


def cleanup_old_history() -> None:
    """Prune old audit rows so the local SQLite database does not grow forever."""
    if SETTINGS.retention_days <= 0:
        return
    cutoff = (
        datetime.now(UTC) - timedelta(days=SETTINGS.retention_days)
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
        LOGGER.warning("retention cleanup skipped due to related rows: %s", exc)
        return

    if any(deleted.values()):
        LOGGER.info("retention cleanup pruned rows older than %s: %s", cutoff, deleted)
        record_processing_step(
            step="retention_cleanup",
            status=StepStatus.DONE,
            detail={"cutoff": cutoff, "deleted": deleted},
        )


def schedule_recoverable_events() -> None:
    rows = load_recoverable_events()
    if not rows:
        return
    LOGGER.info("recovering %d incomplete Resend events", len(rows))
    for row in rows:
        svix_id = str(row["svix_id"] or "")
        email_id = str(row["email_id"] or "")
        try:
            event = json.loads(str(row["verified_json"] or "{}"))
        except json.JSONDecodeError as exc:
            LOGGER.warning(
                "cannot recover event %s: invalid stored JSON: %s", svix_id, exc
            )
            record_processing_step(
                step="event_recovery",
                status=StepStatus.FAILED,
                svix_id=svix_id,
                email_id=email_id or None,
                error=f"invalid stored event JSON: {exc}",
            )
            continue
        schedule_event_task(event, svix_id)
        record_processing_step(
            step="event_recovery",
            status=StepStatus.SCHEDULED,
            svix_id=svix_id,
            email_id=email_id or None,
            detail={"previous_status": row["status"]},
        )


def load_recoverable_events() -> list[sqlite3.Row]:
    recoverable = [EventStatus.PENDING]
    if SETTINGS.recover_failed_events:
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
                (SETTINGS.event_recovery_limit,),
            )
        )


def schedule_event_task(event: dict[str, Any], svix_id: str) -> None:
    task = asyncio.create_task(process_event_safe(event, svix_id))
    RECOVERY_TASKS.add(task)
    task.add_done_callback(RECOVERY_TASKS.discard)


async def startup() -> None:
    SETTINGS.attachment_dir.mkdir(parents=True, exist_ok=True)
    BOT_REPLY_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    harden_storage_permissions()
    cleanup_old_history()
    schedule_recoverable_events()
    LOGGER.info(
        "bridge ready on inbound address %s and owner address %s",
        SETTINGS.inbound_address,
        SETTINGS.owner_address,
    )


def db_health() -> dict[str, Any]:
    try:
        with open_db() as conn:
            conn.execute("SELECT 1").fetchone()
            row = conn.execute("PRAGMA user_version").fetchone()
            schema_version = int(row[0] if row else 0)
        return {"ok": True, "schema_version": schema_version}
    except Exception as exc:
        return {"ok": False, "error": exception_message(exc)}


def request_bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-resend-bridge-secret", "").strip()


def verify_send_authorization(request: Request) -> None:
    token = request_bearer_token(request)
    if not token or not hmac.compare_digest(token, SETTINGS.bridge_send_secret):
        raise HTTPException(status_code=401, detail="invalid send authorization")


@app.post("/send")
async def send_email(request: Request) -> dict[str, Any]:
    if not SETTINGS.resend_api_key:
        raise HTTPException(status_code=503, detail="RESEND_API_KEY is not configured")
    verify_send_authorization(request)
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    payload = normalize_send_payload(
        raw, allow_bot_sender=_is_authorized_bot_reply(raw)
    )
    email_id = (
        str(raw.get("email_id") or raw.get("auto_reply_email_id") or "").strip() or None
    )
    outbound_id, resend_id = await send_resend_email(payload, email_id=email_id)
    return {
        "ok": True,
        "outbound_id": outbound_id,
        "resend_id": resend_id,
        "from": payload["from"],
        "to": payload["to"],
        "cc": payload.get("cc", []),
        "subject": payload["subject"],
        "attachments": [
            outbound_attachment_audit_metadata(item)
            for item in ensure_list(payload.get("attachments"))
            if isinstance(item, dict)
        ],
    }


@app.post("/webhooks/resend")
async def resend_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    if not SETTINGS.resend_webhook_secret:
        raise HTTPException(
            status_code=503, detail="RESEND_WEBHOOK_SECRET is not configured"
        )

    raw_body = await request.body()
    event = verify_resend_webhook(raw_body, request)
    event_type = event.get("type")
    data = event.get("data") or {}
    email_id = str(data.get("email_id") or "").strip()
    svix_id = (
        request.headers.get("svix-id") or f"missing-svix-id:{email_id or 'unknown'}"
    )
    record_webhook_event(
        svix_id=svix_id,
        event_type=str(event_type or ""),
        email_id=email_id or None,
        raw_body=raw_body,
        event=event,
        headers_json=request_headers_json(request),
        ignored=event_type != "email.received",
    )
    if event_type != "email.received":
        record_processing_step(
            step="webhook",
            status=StepStatus.IGNORED,
            svix_id=svix_id,
            email_id=email_id or None,
            detail={"event_type": event_type},
        )
        return {"ok": True, "ignored": event_type}

    if not email_id:
        raise HTTPException(
            status_code=400, detail="email_id missing from webhook payload"
        )

    queued = record_pending_event(svix_id=svix_id, email_id=email_id)
    record_processing_step(
        step="webhook",
        status=StepStatus.QUEUED if queued else StepStatus.DUPLICATE,
        svix_id=svix_id,
        email_id=email_id,
    )
    if queued:
        background_tasks.add_task(process_event_safe, event, svix_id)
    return {"ok": True, "queued": queued, "email_id": email_id}


def verify_resend_webhook(raw_body: bytes, request: Request) -> dict[str, Any]:
    headers = {
        "svix-id": request.headers.get("svix-id", ""),
        "svix-timestamp": request.headers.get("svix-timestamp", ""),
        "svix-signature": request.headers.get("svix-signature", ""),
    }
    try:
        verified = Webhook(SETTINGS.resend_webhook_secret).verify(
            raw_body.decode("utf-8"),
            headers,
        )
    except Exception as exc:
        LOGGER.warning("invalid Resend webhook signature: %s", exc)
        raise HTTPException(
            status_code=400, detail="invalid webhook signature"
        ) from exc

    if isinstance(verified, dict):
        return verified
    return json.loads(verified)


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
        LOGGER.info(
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


def record_fetched_attachment_metadata(
    email_id: str, attachments: list[dict[str, Any]]
) -> None:
    for attachment in attachments:
        item = {
            "id": attachment.get("id"),
            "filename": attachment.get("filename")
            or attachment.get("id")
            or "attachment",
            "content_type": attachment.get("content_type"),
            "size": int(attachment.get("size") or 0),
            "relevant": is_relevant_attachment(attachment),
        }
        record_attachment_history(
            email_id=email_id, raw_attachment=attachment, item=item
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


async def process_event_safe(event: dict[str, Any], svix_id: str) -> None:
    email_id = str((event.get("data") or {}).get("email_id") or "")
    try:
        await process_event(event, svix_id)
        mark_event_done(svix_id)
        record_processing_step(
            step="event",
            status=StepStatus.DONE,
            svix_id=svix_id,
            email_id=email_id or None,
        )
    except Exception as exc:
        LOGGER.exception("failed to process Resend inbound event")
        mark_event_failed(svix_id, exc)
        if email_id:
            update_inbound_status(email_id, InboundStatus.FAILED, str(exc)[:1000])
        record_processing_step(
            step="event",
            status=StepStatus.FAILED,
            svix_id=svix_id,
            email_id=email_id or None,
            error=str(exc)[:1000],
        )
        await notify_telegram(
            "Resend inbound processing failed.\n"
            f"Email ID: {email_id or None}\n"
            f"Error: {exc}",
            email_id=email_id or None,
        )


async def process_event(event: dict[str, Any], svix_id: str) -> None:
    if not SETTINGS.resend_api_key:
        raise RuntimeError("RESEND_API_KEY is not configured")

    event_data = event.get("data") or {}
    email_id = str(event_data["email_id"])
    email, attachments, to_bot = await fetch_and_record_inbound(
        event, svix_id, email_id
    )

    if not to_bot:
        await notify_non_bot_email(email_id, email, attachments)
        return

    await notify_bot_email_received(email_id, email, attachments)
    _write_bot_reply_context(
        email_id,
        sender=bot_sender_address(),
        reply_to=str(email.get("from") or ""),
    )
    try:
        decision = await decide_bot_email(email_id, email, attachments)
        await handle_hermes_decision(email_id, email, decision)
    finally:
        _delete_bot_reply_context(email_id)


async def fetch_and_record_inbound(
    event: dict[str, Any],
    svix_id: str,
    email_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    event_data = event.get("data") or {}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        email = await resend_client.fetch_received_email(
            client,
            email_id,
            api_key=SETTINGS.resend_api_key,
            user_agent=USER_AGENT,
        )
        attachments = await resend_client.fetch_received_attachments(
            client,
            email_id,
            api_key=SETTINGS.resend_api_key,
            user_agent=USER_AGENT,
        )
        if not attachments:
            attachments = (
                email.get("attachments") or event_data.get("attachments") or []
            )

    to_bot = is_to_inbound_address(email, event_data)
    record_inbound_email(
        svix_id=svix_id,
        event=event,
        email=email,
        attachments=attachments,
        addressed_to_inbound=to_bot,
    )
    record_fetched_attachment_metadata(email_id, attachments)
    record_processing_step(
        step="fetch_email",
        status=StepStatus.DONE,
        svix_id=svix_id or None,
        email_id=email_id,
        detail={"attachment_count": len(attachments), "addressed_to_inbound": to_bot},
    )
    return email, attachments, to_bot


async def notify_non_bot_email(
    email_id: str,
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> None:
    LOGGER.info(
        "email %s is not addressed to %s; forwarding to Telegram",
        email_id,
        SETTINGS.inbound_address,
    )
    notice = render_inbound_email_notice(
        email,
        attachments,
        title="主人你有一封新邮件~",
        domain=SETTINGS.resend_domain,
    )
    await notify_telegram(notice, email_id=email_id)
    update_inbound_status(email_id, InboundStatus.NOTIFIED)


async def notify_bot_email_received(
    email_id: str,
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> None:
    """Immediately show the original bot-addressed email before processing it."""

    title = NOTIFICATION_BOT_TITLE.format(AI_NAME=SETTINGS.ai_name)
    await notify_telegram(
        render_inbound_email_notice(
            email,
            attachments,
            title=title,
            domain=SETTINGS.resend_domain,
        ),
        email_id=email_id,
    )
    update_inbound_status(email_id, InboundStatus.PROCESSING)


async def decide_bot_email(
    email_id: str,
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        downloaded = await download_relevant_attachments(client, email_id, attachments)

    LOGGER.info(
        "email %s is addressed to %s; running Hermes task",
        email_id,
        SETTINGS.inbound_address,
    )
    decision = await run_hermes_email_task(
        email=email,
        attachments=attachments,
        downloaded=downloaded,
        email_id=email_id,
    )
    decision["_downloaded_files"] = downloaded
    return decision


async def handle_hermes_decision(
    email_id: str,
    email: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    if not isinstance(decision, dict):
        decision = fallback_notify_decision(
            str(decision),
            "Hermes decision was missing or not a JSON object.",
        )
    action = str(decision.get("action", "notify")).lower()
    reply_payload: dict[str, Any] | None = None
    reply_id: str | None = None
    if action == "reply" and not reply_text_from_decision(decision):
        decision["action"] = "notify"
        decision["owner_report"] = (
            str(decision.get("owner_report") or "").strip()
            + " Hermes chose reply but did not provide a reply body, so the bridge skipped the email reply."
        ).strip()
        action = "notify"
    if action == "reply":
        LOGGER.info("Hermes chose reply for email %s", email_id)
        reply_payload = build_resend_reply_payload(email, decision)
        reply_id = await send_resend_reply(
            email,
            decision,
            email_id=email_id,
            reply_payload=reply_payload,
        )
        await notify_telegram(
            render_processing_result_notice(
                "Hermes 已通过 Resend 自动回复。",
                decision=decision,
                domain=SETTINGS.resend_domain,
                reply_payload=reply_payload,
                reply_id=reply_id,
            ),
            email_id=email_id,
        )
        update_inbound_status(email_id, InboundStatus.REPLIED)
    else:
        LOGGER.info("Hermes chose no email reply for email %s", email_id)
        update_inbound_status(email_id, InboundStatus.NOTIFIED)

    owner_report_attachments = _validate_agent_attachment_paths(
        ensure_list(decision.get("owner_report_attachments") or [])
    )

    await notify_telegram(
        build_activity_summary(
            email,
            decision,
            reply_payload=reply_payload,
            reply_id=reply_id,
        ),
        email_id=email_id,
        attachment_paths=owner_report_attachments,
    )


async def download_relevant_attachments(
    client: httpx.AsyncClient,
    email_id: str,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_dir = SETTINGS.attachment_dir / safe_filename(email_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(0o700)
    results: list[dict[str, Any]] = []

    for attachment in attachments:
        filename = str(
            attachment.get("filename") or attachment.get("id") or "attachment"
        )
        size = int(attachment.get("size") or 0)
        relevant = is_relevant_attachment(attachment)
        item: dict[str, Any] = {
            "id": attachment.get("id"),
            "filename": filename,
            "content_type": attachment.get("content_type"),
            "size": size,
            "relevant": relevant,
        }
        results.append(item)

        download_url = attachment.get("download_url")
        if not relevant or not download_url:
            record_attachment_history(
                email_id=email_id, raw_attachment=attachment, item=item
            )
            continue
        if size and size > SETTINGS.max_attachment_bytes:
            item["skipped"] = f"larger than {SETTINGS.max_attachment_bytes} bytes"
            record_attachment_history(
                email_id=email_id, raw_attachment=attachment, item=item
            )
            continue

        path = unique_path(target_dir / safe_filename(filename))
        tmp_path = path.with_name(f"{path.name}.part")
        try:
            total = 0
            async with client.stream(
                "GET",
                str(download_url),
                headers={"User-Agent": USER_AGENT},
                timeout=90,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        declared_size = 0
                    if declared_size > SETTINGS.max_attachment_bytes:
                        raise ValueError(
                            f"larger than {SETTINGS.max_attachment_bytes} bytes"
                        )
                with tmp_path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > SETTINGS.max_attachment_bytes:
                            raise ValueError(
                                f"larger than {SETTINGS.max_attachment_bytes} bytes"
                            )
                        output.write(chunk)
            tmp_path.replace(path)
            path.chmod(0o600)
            item["size"] = total
            item["local_path"] = str(path)

            snippet = read_text_snippet(path, attachment)
            if snippet:
                item["text_snippet"] = snippet
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning(
                    "could not remove partial attachment download %s", tmp_path
                )
            item["error"] = str(exc)[:1000]
            record_attachment_history(
                email_id=email_id, raw_attachment=attachment, item=item
            )
            raise

        record_attachment_history(
            email_id=email_id, raw_attachment=attachment, item=item
        )

    return results


def is_relevant_attachment(attachment: dict[str, Any]) -> bool:
    filename = str(attachment.get("filename") or "").lower()
    content_type = str(attachment.get("content_type") or "").lower()
    disposition = str(attachment.get("content_disposition") or "").lower()
    suffix = Path(filename).suffix
    if content_type.startswith("image/"):
        return True
    if disposition == "attachment":
        return True
    if suffix in RELEVANT_EXTENSIONS:
        return True
    if filename and disposition != "inline":
        return True
    if filename and disposition == "inline":
        return True
    return bool(content_type and not content_type.startswith("image/"))


def read_text_snippet(
    path: Path, attachment: dict[str, Any], limit: int = 6000
) -> str | None:
    content_type = str(attachment.get("content_type") or "").lower()
    if path.suffix.lower() not in TEXT_EXTENSIONS and not (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/xml"}
    ):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def safe_filename(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    clean = clean.strip("._")
    return clean or "attachment"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for idx in range(1, 1000):
        candidate = path.with_name(f"{stem}-{idx}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not create unique path for {path}")


def is_to_inbound_address(email: dict[str, Any], event_data: dict[str, Any]) -> bool:
    recipients: list[str] = []
    for source in (email, event_data):
        for key in ("to", "cc", "bcc"):
            value = source.get(key)
            if isinstance(value, list):
                recipients.extend(str(item) for item in value)
            elif value:
                recipients.append(str(value))
    parsed = {parseaddr(item)[1].lower() or item.lower() for item in recipients}
    return SETTINGS.inbound_address in parsed


async def send_resend_email(
    payload: dict[str, Any],
    *,
    email_id: str | None = None,
    step: str = "resend_send",
) -> tuple[int, str]:
    outbound_id = create_outbound_message(
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
                api_key=SETTINGS.resend_api_key,
                user_agent=USER_AGENT,
            )
        resend_id = str(response_body.get("id") or "")
        update_outbound_message(
            outbound_id,
            status=OutboundStatus.SENT,
            response=response_body,
            external_id=resend_id,
        )
        record_processing_step(
            step=step,
            status=StepStatus.SENT,
            email_id=email_id,
            detail={"resend_id": resend_id, "outbound_id": outbound_id},
        )
        return outbound_id, resend_id
    except resend_client.ResendAPIError as exc:
        response_body = exc.response_body
        update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
            response=response_body,
            error=str(exc)[:1000],
        )
        record_processing_step(
            step=step,
            status=StepStatus.FAILED,
            email_id=email_id,
            error=str(exc)[:1000],
        )
        raise
    except Exception as exc:
        update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
            response=response_body,
            error=str(exc)[:1000],
        )
        record_processing_step(
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
    payload = normalize_send_payload(raw_payload, allow_bot_sender=True)
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
        LOGGER.warning("skipping invalid reply attachment: %s", message)

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
            LOGGER.warning("could not create missing reply attachment %s: %s", path, exc)
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
            path = APP_DIR / path
        path = path.resolve()
        if not any(_path_is_relative_to(path, root) for root in GENERATED_ATTACHMENT_ROOTS):
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


def build_activity_summary(
    email: dict[str, Any],
    decision: dict[str, Any],
    *,
    reply_payload: dict[str, Any] | None = None,
    reply_id: str | None = None,
) -> str:
    owner_report = str(decision.get("owner_report") or "").strip()
    return f"**任务总结：**\n\n{owner_report}" if owner_report else "**任务总结：**"


def email_summary(email: dict[str, Any]) -> dict[str, Any]:
    text = str(email.get("text") or "")
    html = str(email.get("html") or "")
    return {
        "id": email.get("id"),
        "created_at": email.get("created_at"),
        "from": email.get("from"),
        "to": email.get("to"),
        "cc": email.get("cc"),
        "subject": email.get("subject"),
        "message_id": email.get("message_id"),
        "headers": email.get("headers"),
        "text_preview": text[:8000],
        "html_preview": html[:4000] if not text else "",
    }