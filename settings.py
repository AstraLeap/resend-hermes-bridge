from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent


def load_project_env() -> None:
    load_dotenv(APP_DIR / ".env")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value.strip()


def hermes_send_bin() -> Path:
    resolved = shutil.which("hermes")
    if resolved:
        return Path(resolved).expanduser()
    home_bin = Path.home() / ".local" / "bin" / "hermes"
    if home_bin.exists():
        return home_bin
    hermes_home_bin = Path.home() / ".hermes" / "bin" / "hermes"
    if hermes_home_bin.exists():
        return hermes_home_bin
    return Path("/usr/local/bin/hermes")


def hermes_home() -> Path:
    return Path.home() / ".hermes"


def hermes_bridge_cache_dir() -> Path:
    return hermes_home() / "cache" / "resend-bridge"


def bridge_data_dir() -> Path:
    return Path(os.getenv("BRIDGE_DATA_DIR", str(APP_DIR / "data"))).expanduser()


def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def generated_attachment_roots() -> list[Path]:
    return [(hermes_bridge_cache_dir() / "generated").resolve()]


def validate_environment() -> list[str]:
    """Collect configuration problems before starting the bridge."""
    errors: list[str] = []

    required = [
        "RESEND_API_KEY",
        "RESEND_WEBHOOK_SECRET",
        "RESEND_DOMAIN",
        "BOT_FROM_LOCAL",
        "OWNER_FROM_LOCAL",
    ]
    for name in required:
        value = os.getenv(name, "").strip()
        if not value:
            errors.append(f"{name} is not set")

    send_bin = hermes_send_bin()
    if not send_bin.exists():
        errors.append(
            f"hermes CLI not found at {send_bin}; install Hermes or ensure it is on PATH"
        )

    return errors


@dataclass(frozen=True)
class Settings:
    resend_api_key: str
    resend_webhook_secret: str
    resend_domain: str
    bot_from_local: str
    owner_from_local: str
    hermes_send_bin: Path
    hermes_bridge_cache_dir: Path
    bridge_db: Path
    attachment_dir: Path
    mcp_drafts_file: Path
    mcp_drafts_lock_file: Path
    max_attachment_bytes: int
    max_outbound_attachment_bytes: int
    hermes_timeout_seconds: float
    retention_days: int
    recover_failed_events: bool
    event_recovery_limit: int
    notification_target: str
    ai_name: str
    bot_reply_context_dir: Path
    generated_attachment_roots: list[Path]

    @property
    def inbound_address(self) -> str:
        return f"{self.bot_from_local}@{self.resend_domain}"

    @property
    def owner_address(self) -> str:
        return f"{self.owner_from_local}@{self.resend_domain}"


def load_settings() -> Settings:
    load_project_env()
    validation_errors = validate_environment()
    if validation_errors:
        message = "Bridge configuration is incomplete:\n\n"
        for error in validation_errors:
            message += f"  - {error}\n"
        message += "\nFix: cp .env.example .env, fill in the values, or run ./scripts/setup.sh"
        raise RuntimeError(message)
    data_dir = bridge_data_dir()
    return Settings(
        resend_api_key=require_env("RESEND_API_KEY"),
        resend_webhook_secret=require_env("RESEND_WEBHOOK_SECRET"),
        resend_domain=require_env("RESEND_DOMAIN").lower(),
        bot_from_local=require_env("BOT_FROM_LOCAL").lower(),
        owner_from_local=require_env("OWNER_FROM_LOCAL").lower(),
        hermes_send_bin=hermes_send_bin(),
        hermes_bridge_cache_dir=hermes_bridge_cache_dir(),
        bridge_db=data_dir / "state.db",
        attachment_dir=data_dir / "attachments",
        mcp_drafts_file=data_dir / "mcp_email_drafts.json",
        mcp_drafts_lock_file=data_dir / "mcp_email_drafts.json.lock",
        max_attachment_bytes=int(os.getenv("MAX_ATTACHMENT_DOWNLOAD_BYTES", "15728640")),
        max_outbound_attachment_bytes=int(
            os.getenv("MAX_OUTBOUND_ATTACHMENT_BYTES", "31457280")
        ),
        hermes_timeout_seconds=float(os.getenv("HERMES_TIMEOUT_SECONDS", "180")),
        retention_days=int(os.getenv("BRIDGE_RETENTION_DAYS", "90")),
        recover_failed_events=env_bool("BRIDGE_RECOVER_FAILED_EVENTS", "true"),
        event_recovery_limit=int(os.getenv("BRIDGE_EVENT_RECOVERY_LIMIT", "50")),
        notification_target=os.getenv("NOTIFICATION_TARGET", "telegram").strip(),
        ai_name=os.getenv("AI_NAME", "卡宝").strip(),
        bot_reply_context_dir=Path(
            os.getenv("BOT_REPLY_CONTEXT_DIR", str(data_dir / "bot_reply_contexts"))
        ),
        generated_attachment_roots=generated_attachment_roots(),
    )
