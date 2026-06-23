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

info "== Resend Hermes Bridge Setup =="

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

# Helper to read or update a value in .env
set_env_value() {
    local key="$1"
    local value="$2"
    if grep -qE "^#?\s*${key}=" "$ENV_FILE"; then
        # Replace existing line (commented or not)
        sed -i "s|^#\?\s*${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

read_env_value() {
    local key="$1"
    local default="${2:-}"
    local current
    current="$(grep -E "^#?\s*${key}=" "$ENV_FILE" | tail -1 | sed "s|^#\?\s*${key}=||" || true)"
    if [[ -z "$current" ]] || [[ "$current" == "re_replace_me" ]] || [[ "$current" == "whsec_replace_me" ]] || [[ "$current" == "change-me-generate-with-openssl-rand-hex-32" ]]; then
        current="$default"
    fi
    echo "$current"
}

prompt() {
    local key="$1"
    local label="$2"
    local default="$3"
    local is_secret="${4:-false}"
    local current="$(read_env_value "$key" "$default")"
    local prompt_text="$label"
    if [[ -n "$current" ]] && [[ "$current" != "$default" ]]; then
        prompt_text="$label [$current]"
    fi

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
prompt "RESEND_API_KEY" "Resend API key" ""
prompt "RESEND_WEBHOOK_SECRET" "Resend webhook signing secret" ""
prompt "RESEND_DOMAIN" "Verified Resend sender domain (without @)" "example.com"
prompt "BOT_FROM_LOCAL" "Bot inbox local part (e.g. bot)" "bot"
prompt "OWNER_FROM_LOCAL" "Owner inbox local part (e.g. mail)" "mail"
prompt "AI_NAME" "Display name for owner notices" "Hermes"
prompt "NOTIFICATION_TARGET" "Notification platform (telegram/weixin/qqbot/wecom/discord/slack/signal)" "telegram"

# Generate bridge send secret if still placeholder
bridge_secret="$(read_env_value "RESEND_BRIDGE_SEND_SECRET" "")"
if [[ -z "$bridge_secret" ]] || [[ "$bridge_secret" == "change-me-generate-with-openssl-rand-hex-32" ]]; then
    bridge_secret="$(openssl rand -hex 32 2>/dev/null || "$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(32))')"
    set_env_value "RESEND_BRIDGE_SEND_SECRET" "$bridge_secret"
    ok "Generated RESEND_BRIDGE_SEND_SECRET"
fi

# Check Hermes availability
if command -v hermes >/dev/null 2>&1; then
    ok "Hermes CLI found: $(command -v hermes)"
elif [[ -x "$HOME/.local/bin/hermes" ]]; then
    ok "Hermes CLI found: $HOME/.local/bin/hermes"
else
    warn "Hermes CLI not found on PATH, ~/.local/bin, or /usr/local/bin"
    warn "Make sure Hermes is installed before running the bridge"
fi

# Optional: install Hermes host proxy systemd user service
if command -v systemctl >/dev/null 2>&1; then
    read -rp "Install Hermes host proxy systemd user service? [y/N] " install_proxy
    if [[ "$install_proxy" =~ ^[Yy]$ ]]; then
        mkdir -p "$HOME/.config/systemd/user"
        cat > "$HOME/.config/systemd/user/hermes-send-proxy.service" <<EOF
[Unit]
Description=Hermes host proxy for Resend Hermes Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment="PATH=$HOME/.hermes/bin:$HOME/.hermes/hermes-agent/venv/bin:$HOME/.hermes/hermes-agent/node_modules/.bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="HERMES_HOME=$HOME/.hermes"
Environment="HERMES_PROXY_HOST=127.0.0.1"
Environment="HERMES_PROXY_SECRET=$bridge_secret"
ExecStart=$VENV_DIR/bin/python scripts/hermes_send_proxy.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        ok "Installed host proxy service. Start with: systemctl --user enable --now hermes-send-proxy.service"
    fi
fi

# Optional: install MCP config into Hermes
read -rp "Install MCP server config into Hermes config.yaml? [y/N] " install_mcp
if [[ "$install_mcp" =~ ^[Yy]$ ]]; then
    HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
    "$VENV_DIR/bin/python" -m bridge_admin install-mcp || warn "MCP install failed; you can run it manually later"
fi

ok "Setup complete. Edit $ENV_FILE if needed, then start the bridge."
info "Start the host proxy: systemctl --user enable --now hermes-send-proxy.service"
info "Run the bridge with Docker: docker compose up -d --build"
