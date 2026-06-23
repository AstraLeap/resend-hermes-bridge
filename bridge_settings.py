from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
SEND_SECRET_PLACEHOLDERS = {
    "change-me-generate-with-openssl-rand-hex-32",
    "generate-with-openssl-rand-hex-32",
}


def load_project_env() -> None:
    load_dotenv(APP_DIR / ".env")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Environment variable {name} is required but not set.")
    return value.strip()


def require_secret_env(name: str) -> str:
    value = require_env(name)
    if value in SEND_SECRET_PLACEHOLDERS:
        raise RuntimeError(
            f"Environment variable {name} must be replaced with a generated secret. "
            "Use: openssl rand -hex 32"
        )
    return value


def hermes_send_bin() -> Path:
    value = os.getenv("HERMES_SEND_BIN", "").strip()
    if value:
        return Path(value).expanduser()
    resolved = shutil.which("hermes")
    if resolved:
        return Path(resolved).expanduser()
    home_bin = Path.home() / ".local" / "bin" / "hermes"
    if home_bin.exists():
        return home_bin
    return Path("/usr/local/bin/hermes")


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def strip_simple_yaml_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"} and value[-1:] == value[0]:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def read_hermes_config() -> dict[str, str]:
    path = hermes_home() / "config.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Hermes config not found at {path}") from exc

    config: dict[str, str] = {}
    wanted = {
        "API_SERVER_ENABLED",
        "API_SERVER_HOST",
        "API_SERVER_PORT",
        "API_SERVER_KEY",
    }
    for raw_line in lines:
        if not raw_line or raw_line[0].isspace():
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in wanted:
            config[key] = strip_simple_yaml_value(value)
    return config


def require_hermes_config(config: dict[str, str], name: str) -> str:
    value = config.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Hermes config value {name} is required in HERMES_HOME/config.yaml"
        )
    return value


def api_server_enabled(config: dict[str, str]) -> bool:
    value = require_hermes_config(config, "API_SERVER_ENABLED").lower()
    return value in {"1", "true", "yes", "y", "on"}


def hermes_api_url() -> str:
    config = read_hermes_config()
    if not api_server_enabled(config):
        raise RuntimeError("Hermes API server must be enabled in HERMES_HOME/config.yaml")
    host = require_hermes_config(config, "API_SERVER_HOST")
    port = require_hermes_config(config, "API_SERVER_PORT")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/v1/chat/completions"


def hermes_api_key() -> str:
    config = read_hermes_config()
    if not api_server_enabled(config):
        raise RuntimeError("Hermes API server must be enabled in HERMES_HOME/config.yaml")
    return require_hermes_config(config, "API_SERVER_KEY")


def env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y"}


def generated_attachment_roots() -> list[Path]:
    raw = os.getenv("GENERATED_ATTACHMENT_ROOTS", "").strip()
    if raw:
        return [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]
    return [Path.home().expanduser().resolve() / ".hermes" / "cache"]


@dataclass(frozen=True)
class Settings:
    resend_api_key: str
    resend_webhook_secret: str
    bridge_send_secret: str
    resend_domain: str
    bot_from_local: str
    owner_from_local: str
    hermes_send_bin: Path
    hermes_api_url: str
    hermes_api_key: str
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
    user_agent: str
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
    return Settings(
        resend_api_key=require_env("RESEND_API_KEY"),
        resend_webhook_secret=require_env("RESEND_WEBHOOK_SECRET"),
        bridge_send_secret=require_secret_env("RESEND_BRIDGE_SEND_SECRET"),
        resend_domain=require_env("RESEND_DOMAIN").lower(),
        bot_from_local=require_env("BOT_FROM_LOCAL").lower(),
        owner_from_local=require_env("OWNER_FROM_LOCAL").lower(),
        hermes_send_bin=hermes_send_bin(),
        hermes_api_url=hermes_api_url(),
        hermes_api_key=hermes_api_key(),
        bridge_db=APP_DIR / "state.db",
        attachment_dir=APP_DIR / "attachments",
        mcp_drafts_file=APP_DIR / "mcp_email_drafts.json",
        mcp_drafts_lock_file=APP_DIR / "mcp_email_drafts.json.lock",
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
        user_agent=os.getenv("BRIDGE_USER_AGENT", "resend-hermes-bridge/1.0"),
        bot_reply_context_dir=Path(
            os.getenv("BOT_REPLY_CONTEXT_DIR", str(APP_DIR / "bot_reply_contexts"))
        ),
        generated_attachment_roots=generated_attachment_roots(),
    )
