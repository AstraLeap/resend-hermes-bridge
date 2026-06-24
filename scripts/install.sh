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

HERMES_CONFIG="$HOME/.hermes/config.yaml"
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
    [[ -z "$value" ]] ||
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
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

read_env_value() {
    local key="$1"
    local default="${2:-}"
    local current
    current="$(grep -E "^[[:space:]]*#?[[:space:]]*${key}=" "$ENV_FILE" | tail -1 || true)"
    current="${current#*=}"
    if is_placeholder "$current"; then
        current="$default"
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
    local prompt_text="$label"
    if [[ -n "$current" ]] && [[ "$current" != "$default" ]]; then
        prompt_text="$label [$current]"
    fi

    local value=""
    if [[ "$is_secret" == "true" ]]; then
        read -rsp "$prompt_text: " value
        echo
    else
        read -rp "$prompt_text: " value
    fi

    if [[ -z "$value" ]]; then
        value="$current"
    fi
    set_env_value "$key" "$value"
}

info "Please fill in the required configuration:"
prompt "RESEND_API_KEY" "Resend API key" "" true
prompt "RESEND_WEBHOOK_SECRET" "Resend webhook signing secret" "" true
prompt "RESEND_DOMAIN" "Verified Resend sender domain (without @)" "example.com"
prompt "BOT_FROM_LOCAL" "Bot inbox local part (e.g. bot)" "bot"
prompt "OWNER_FROM_LOCAL" "Owner inbox local part (e.g. mail)" "mail"
prompt "AI_NAME" "Display name for owner notices" "Hermes"
prompt "NOTIFICATION_TARGET" "Notification platform (telegram/weixin/qqbot/wecom/discord/slack/signal)" "telegram"

info "Installing resend-hermes-bridge systemd user service..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/resend-hermes-bridge.service" <<EOF
[Unit]
Description=Resend Hermes Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="PATH=$HOME/.hermes/bin:$HOME/.hermes/hermes-agent/venv/bin:$HOME/.hermes/hermes-agent/node_modules/.bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/uvicorn app:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now resend-hermes-bridge.service
ok "Installed and started bridge service"

info "Registering MCP server in Hermes config.yaml..."
if "$VENV_DIR/bin/python" "$ROOT_DIR/scripts/manage.py" install-mcp; then
    ok "MCP server registered as resend_email"
else
    err "MCP install failed"
    err "Fix Hermes config, then rerun this installer"
    exit 1
fi

ok "Install complete. Edit $ENV_FILE if needed."
info "If a Hermes session is already open, run /reload-mcp in that session."
