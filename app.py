from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx  # noqa: F401
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

import settings as bridge_settings
from db.operations import (  # noqa: F401
    cleanup_old_history,
    create_outbound_message,
    db_health,
    ensure_schema_version,
    harden_storage_permissions,
    init_db,
    json_dumps,
    load_recoverable_events,
    mark_event_done,
    mark_event_failed,
    now_iso,
    open_db,
    record_attachment_history,
    record_hermes_decision,
    record_inbound_email,
    record_pending_event,
    record_processing_step,
    record_webhook_event,
    update_inbound_status,
    update_outbound_message,
)
from db.state import EventStatus, InboundStatus, OutboundStatus, StepStatus  # noqa: F401
from services.hermes_client import (  # noqa: F401
    build_hermes_task_prompt,
    coerce_bool,
    decode_common_json_escapes,
    fallback_notify_decision,
    parse_json_decision,
    parse_loose_decision_object,
    parse_loose_string_value,
    run_hermes_email_task,
    run_hermes_proxy_task,
    run_hermes_task,
    strip_json_code_fence,
)
from services.inbound_email import (  # noqa: F401
    build_activity_summary,
    copy_attachment_to_hermes_cache,
    decide_bot_email,
    download_relevant_attachments,
    email_summary,
    fetch_and_record_inbound,
    handle_hermes_decision,
    is_relevant_attachment,
    is_to_inbound_address,
    notify_bot_email_received,
    notify_non_bot_email,
    process_event,
    process_event_safe,
    read_text_snippet,
    record_fetched_attachment_metadata,
)
from services.notification import notify_telegram  # noqa: F401
from services.resend_outbound import (  # noqa: F401
    AttachmentSpec,
    HermesDecision,
    SendRequest,
    build_resend_reply_payload,
    normalize_outbound_attachments,
    outbound_attachment_audit_metadata,
    outbound_payload_for_audit,
    reply_attachment_specs_from_decision,
    reply_text_from_decision,
    send_resend_email,
    send_resend_reply,
)
from settings import Settings
from utils.email_core import (
    EmailValidationError,
    clean_header_value,
    ensure_list,
    parse_email_addresses,
)
from utils.email_core import (
    resolve_sender as resolve_email_sender,
)

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
HERMES_BRIDGE_CACHE_DIR = SETTINGS.hermes_bridge_cache_dir


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await startup()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    global SETTINGS, USER_AGENT, BOT_REPLY_CONTEXT_DIR, GENERATED_ATTACHMENT_ROOTS
    global HERMES_BRIDGE_CACHE_DIR
    if settings is not None:
        SETTINGS = settings
        USER_AGENT = settings.user_agent
        BOT_REPLY_CONTEXT_DIR = settings.bot_reply_context_dir
        GENERATED_ATTACHMENT_ROOTS = settings.generated_attachment_roots
        HERMES_BRIDGE_CACHE_DIR = settings.hermes_bridge_cache_dir
    from routers import health, send, webhooks

    fast_app = FastAPI(title="Resend Hermes Bridge", lifespan=lifespan)
    fast_app.include_router(health.router)
    fast_app.include_router(send.router)
    fast_app.include_router(webhooks.router)
    return fast_app


app = create_app()
RECOVERY_TASKS: set[asyncio.Task] = set()


def bot_sender_address() -> str:
    return SETTINGS.inbound_address


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


def _path_mapping_pairs() -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    if SETTINGS.hermes_host_home is not None:
        pairs.append(
            (
                bridge_settings.hermes_home().expanduser().resolve(),
                SETTINGS.hermes_host_home.expanduser().resolve(),
            )
        )
    return pairs


def _map_path_between_roots(path: Path, mappings: list[tuple[Path, Path]]) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    for source_root, target_root in mappings:
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            continue
        return target_root / relative
    return resolved


def host_path_for_bridge_path(raw_path: str | Path) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return str(path)
    return str(_map_path_between_roots(path, _path_mapping_pairs()))


def bridge_path_for_host_path(raw_path: str | Path) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return str(path)
    reverse_pairs = [
        (host_root, bridge_root) for bridge_root, host_root in _path_mapping_pairs()
    ]
    return str(_map_path_between_roots(path, reverse_pairs))


def hermes_bridge_inbound_dir(email_id: str) -> Path:
    return HERMES_BRIDGE_CACHE_DIR / "inbound" / safe_filename(email_id)


def agent_attachment_roots() -> list[Path]:
    roots = [HERMES_BRIDGE_CACHE_DIR]
    roots.extend(GENERATED_ATTACHMENT_ROOTS)
    return [root.expanduser().resolve() for root in roots]


def _hermes_cache_root() -> Path:
    return (bridge_settings.hermes_home() / "cache").expanduser().resolve(strict=False)


def _host_cache_owner() -> tuple[int, int] | None:
    if SETTINGS.hermes_host_home is None:
        return None
    try:
        stat_result = _hermes_cache_root().stat()
    except OSError:
        return None
    return stat_result.st_uid, stat_result.st_gid


def apply_host_cache_permissions(path: Path, *, directory: bool) -> None:
    resolved = path.expanduser().resolve(strict=False)
    cache_root = _hermes_cache_root()
    if not _path_is_relative_to(resolved, cache_root):
        return

    owner = _host_cache_owner()
    if owner is not None:
        try:
            os.chown(resolved, owner[0], owner[1])
        except OSError:
            LOGGER.warning("could not chown Hermes cache path %s", resolved)
    try:
        resolved.chmod(0o700 if directory else 0o600)
    except OSError:
        LOGGER.warning("could not chmod Hermes cache path %s", resolved)


def prepare_hermes_cache_permissions() -> None:
    for path in [HERMES_BRIDGE_CACHE_DIR, *GENERATED_ATTACHMENT_ROOTS]:
        apply_host_cache_permissions(path, directory=True)


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


def schedule_event_task(event: dict[str, Any], svix_id: str) -> None:
    task = asyncio.create_task(process_event_safe(event, svix_id))
    RECOVERY_TASKS.add(task)
    task.add_done_callback(RECOVERY_TASKS.discard)


async def startup() -> None:
    SETTINGS.attachment_dir.mkdir(parents=True, exist_ok=True)
    HERMES_BRIDGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for root in GENERATED_ATTACHMENT_ROOTS:
        root.mkdir(parents=True, exist_ok=True)
    BOT_REPLY_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    harden_storage_permissions()
    prepare_hermes_cache_permissions()
    cleanup_old_history()
    schedule_recoverable_events()
    LOGGER.info(
        "bridge ready on inbound address %s and owner address %s",
        SETTINGS.inbound_address,
        SETTINGS.owner_address,
    )


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
