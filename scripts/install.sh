#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.example"

info() { printf '\033[1;34m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err() { printf '\033[1;31m%s\033[0m\n' "$*"; }

info "== Resend Hermes Bridge Install =="

HERMES_CANDIDATES=("$HOME/.local/bin/hermes" "$HOME/.hermes/bin/hermes" "/usr/local/bin/hermes")
HERMES_FOUND=""

# Hermes must be installed and configured before the bridge install can proceed.
if command -v hermes >/dev/null 2>&1; then
  HERMES_FOUND="$(command -v hermes)"
else
  for candidate in "${HERMES_CANDIDATES[@]}"; do
    if [[ -x "$candidate" ]]; then
      HERMES_FOUND="$candidate"
      break
    fi
  done
fi

if [[ -z "$HERMES_FOUND" ]]; then
  err "Hermes CLI not found on PATH, ~/.local/bin, ~/.hermes/bin, or /usr/local/bin"
  err "Install and configure Hermes before installing this bridge"
  exit 1
fi

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_CONFIG="$HERMES_HOME/config.yaml"
if [[ ! -f "$HERMES_CONFIG" ]]; then
  err "Hermes config not found at $HERMES_CONFIG"
  err "Run Hermes setup first, then rerun this installer"
  exit 1
fi

ok "Hermes CLI found: $HERMES_FOUND"
ok "Hermes config found: $HERMES_CONFIG"

if ! command -v systemctl >/dev/null 2>&1; then
  err "systemctl not found"
  err "This installer requires systemd user services"
  exit 1
fi
ok "systemctl found"

# Python version check
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  err "Python not found: $PYTHON_BIN"
  exit 1
fi

PY_VERSION="$($PYTHON_BIN -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  err "Python 3.11+ is required, found $PY_VERSION"
  exit 1
fi
ok "Python $PY_VERSION found"

# Create venv and install dependencies
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  info "Creating virtual environment at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

info "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$ROOT_DIR/requirements.txt"
ok "Dependencies installed"

# Copy .env.example if .env is missing
if [[ ! -f "$ENV_FILE" ]]; then
  info "Creating $ENV_FILE from example"
  cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

is_placeholder() {
  local value="$1"
  [[ "$value" == *_replace_me ]] ||
    [[ "$value" == *change-me* ]]
}

# Helper to read or update a value in .env
set_env_value() {
  local key="$1"
  local value="$2"
  if grep -qE "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV_FILE"; then
    sed -E -i "s|^[[:space:]]*#?[[:space:]]*${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >>"$ENV_FILE"
  fi
}

read_env_value() {
  local key="$1"
  local default="${2:-}"
  local current
  current="$(grep -E "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV_FILE" | tail -1 || true)"
  current="${current#*=}"
  if is_placeholder "$current"; then
    current=""
  fi
  echo "$current"
}

prompt() {
  local key="$1"
  local label="$2"
  local default="$3"
  local is_secret="${4:-false}"
  local current
  current="$(read_env_value "$key" "$default")"

  local value=""
  if [[ "$is_secret" == "true" ]]; then
    read -rsp "$label: " value
    echo
  else
    read -rp "$label: " value
  fi

  if [[ -z "$value" ]]; then
    if [[ -n "$current" ]]; then
      return
    fi
    value="$default"
  fi
  set_env_value "$key" "$value"
}

prompt_language() {
  local current
  current="$(read_env_value "BRIDGE_LANGUAGE" "zh")"
  if [[ -z "$current" ]]; then
    current="zh"
  fi

  info "Select bridge language / 选择桥接服务语言 (current: $current):"
  info "  1) 中文 (zh)"
  info "  2) English (en)"
  local choice=""
  read -rp "Enter 1 or 2 [default: 1]: " choice
  case "$choice" in
    2 | en | En | EN | english | English)
      set_env_value "BRIDGE_LANGUAGE" "en"
      ok "Using English"
      ;;
    1 | zh | Zh | ZH | chinese | 中文 | "")
      set_env_value "BRIDGE_LANGUAGE" "zh"
      ok "使用中文 / Using Chinese"
      ;;
    *)
      warn "Unknown choice '$choice', defaulting to Chinese"
      set_env_value "BRIDGE_LANGUAGE" "zh"
      ;;
  esac
}

restart_hermes_gateway() {
  if "$HERMES_FOUND" gateway restart >/dev/null 2>&1; then
    ok "Hermes Gateway restarted"
    return 0
  fi

  if command -v systemctl >/dev/null 2>&1 &&
    systemctl --user list-unit-files hermes-gateway.service >/dev/null 2>&1; then
    if systemctl --user restart hermes-gateway.service; then
      ok "Hermes Gateway restarted via systemd"
      return 0
    fi
  fi

  warn "Could not restart Hermes Gateway automatically; restart it manually for the patch to take effect"
}

patch_telegram_cjk_rich_guard() {
  local adapter_file="$HERMES_HOME/hermes-agent/plugins/platforms/telegram/adapter.py"
  if [[ ! -f "$adapter_file" ]]; then
    warn "Telegram adapter not found at $adapter_file; skipping CJK rich-message patch"
    return 0
  fi

  if "$PYTHON_BIN" - "$adapter_file" <<'PY'; then
from __future__ import annotations

import sys
from pathlib import Path

path = Path(sys.argv[1])
target = "            and not self._has_telegram_desktop_cjk_rich_garble_shape(content)"
patched = (
    "            # and not self._has_telegram_desktop_cjk_rich_garble_shape(content)"
    "  # patched by resend-hermes-bridge install"
)
text = path.read_text(encoding="utf-8")
if patched in text and target not in text:
    raise SystemExit(0)

count = text.count(target)
if count != 2:
    print(f"Expected 2 Telegram CJK rich-message guard lines, found {count}", file=sys.stderr)
    raise SystemExit(1)

path.write_text(text.replace(target, patched), encoding="utf-8")
PY
    return 0
  else
    warn "Could not patch Telegram CJK rich-message guard; leaving Hermes adapter unchanged"
    return 1
  fi
}

enable_telegram_rich_options() {
  if "$VENV_DIR/bin/python" - "$HERMES_CONFIG" <<'PY'; then
from __future__ import annotations

import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
if not isinstance(config, dict):
    raise SystemExit("Hermes config.yaml root must be a mapping")

platforms = config.setdefault("platforms", {})
if not isinstance(platforms, dict):
    raise SystemExit("Hermes config.yaml has an invalid platforms value")

telegram = platforms.setdefault("telegram", {})
if not isinstance(telegram, dict):
    raise SystemExit("Hermes config.yaml has an invalid platforms.telegram value")

extra = telegram.setdefault("extra", {})
if not isinstance(extra, dict):
    raise SystemExit("Hermes config.yaml has an invalid platforms.telegram.extra value")

extra["rich_messages"] = True
extra["rich_drafts"] = True
path.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
PY
    return 0
  else
    warn "Could not enable Telegram rich message options in Hermes config"
    return 1
  fi
}

info "Please fill in the required configuration:"
prompt "RESEND_API_KEY" "Resend API key" "" true
prompt "RESEND_WEBHOOK_SECRET" "Resend webhook signing secret" "" true
prompt "RESEND_DOMAIN" "Verified Resend sender domain (without @)" "example.com"
prompt "BOT_FROM_LOCAL" "Bot inbox local part (e.g. bot)" "bot"
prompt "OWNER_FROM_LOCAL" "Owner inbox local part (e.g. mail)" "mail"
prompt "AI_NAME" "Display name for owner notices" "Hermes"
prompt "RESEND_BRIDGE_PORT" "Resend bridge local port" "8765"
prompt "BOT_SENDER_ALLOWLIST" "Bot sender allowlist, comma-separated (blank allows all)" ""
prompt "NOTIFICATION_TARGET" "Notification platform (telegram/weixin/qqbot/wecom/discord/slack/signal)" "telegram"
prompt_language

info "Installing resend-hermes-bridge systemd user service..."
mkdir -p "$HOME/.config/systemd/user"
cat >"$HOME/.config/systemd/user/resend-hermes-bridge.service" <<EOF
[Unit]
Description=Resend Hermes Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="PATH=$HOME/.hermes/bin:$HOME/.hermes/hermes-agent/venv/bin:$HOME/.hermes/hermes-agent/node_modules/.bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=$ENV_FILE
ExecStart=/bin/sh -c 'exec "$VENV_DIR/bin/uvicorn" app:app --host 127.0.0.1 --port "\$\${RESEND_BRIDGE_PORT:-8765}"'
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now resend-hermes-bridge.service
ok "Installed and started bridge service"

info "Registering MCP server in Hermes config.yaml..."
if mcp_install_output="$("$VENV_DIR/bin/python" "$ROOT_DIR/scripts/manage.py" install-mcp 2>&1)"; then
  ok "MCP server registered as resend_email"
else
  err "MCP install failed"
  printf '%s\n' "$mcp_install_output" >&2
  err "Fix Hermes config, then rerun this installer"
  exit 1
fi

info "Configuring Hermes Telegram rich display..."
telegram_rich_ok=true
patch_telegram_cjk_rich_guard || telegram_rich_ok=false
enable_telegram_rich_options || telegram_rich_ok=false
if [[ "$telegram_rich_ok" == "true" ]]; then
  ok "Telegram rich display configured"
else
  warn "Telegram rich display configuration incomplete"
fi
info "Restarting Hermes Gateway so the Telegram adapter patch takes effect..."
restart_hermes_gateway

ok "Install complete. Edit $ENV_FILE if needed."
