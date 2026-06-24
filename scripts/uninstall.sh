#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
ENV_FILE="$ROOT_DIR/.env"
SERVICE_NAME="resend-hermes-bridge.service"
SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_CONFIG="$HERMES_HOME/config.yaml"

info() { printf '\033[1;34m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
err() { printf '\033[1;31m%s\033[0m\n' "$*"; }

YAML_PYTHON=""
TEMP_YAML_ENV=""

confirm() {
    local question="$1"
    local response
    read -rp "$question [y/N] " response
    [[ "$response" =~ ^[Yy]$ ]]
}

cleanup_temp_yaml_env() {
    if [[ -n "$TEMP_YAML_ENV" && -d "$TEMP_YAML_ENV" ]]; then
        rm -rf "$TEMP_YAML_ENV"
        TEMP_YAML_ENV=""
    fi
}
trap cleanup_temp_yaml_env EXIT

prepare_yaml_python() {
    if python3 -c "import yaml" >/dev/null 2>&1; then
        YAML_PYTHON="python3"
        return 0
    fi
    if [[ -x "$VENV_DIR/bin/python" ]] && "$VENV_DIR/bin/python" -c "import yaml" >/dev/null 2>&1; then
        YAML_PYTHON="$VENV_DIR/bin/python"
        return 0
    fi

    TEMP_YAML_ENV="$(mktemp -d)"
    python3 -m venv "$TEMP_YAML_ENV/venv"
    "$TEMP_YAML_ENV/venv/bin/python" -m pip install -q pyyaml
    YAML_PYTHON="$TEMP_YAML_ENV/venv/bin/python"
}

info "== Resend Hermes Bridge Uninstall =="

# Stop and disable systemd service if present
if [[ -f "$SERVICE_FILE" ]] && command -v systemctl >/dev/null 2>&1; then
    info "Stopping systemd user service..."
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload
    ok "Removed systemd user service"
fi

# Remove MCP config from Hermes config.yaml
if [[ -f "$HERMES_CONFIG" ]] && confirm "Remove resend_email MCP server from Hermes config.yaml?"; then
    if prepare_yaml_python; then
        "$YAML_PYTHON" - "$HERMES_CONFIG" <<'PY'
from __future__ import annotations
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
config = yaml.safe_load(text) or {}
servers = config.get("mcp_servers", {})
removed = []
if isinstance(servers, dict):
    if "resend_email" in servers:
        del servers["resend_email"]
        removed.append("resend_email")
if removed:
    if not servers:
        del config["mcp_servers"]
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"Removed MCP server(s): {', '.join(removed)}")
else:
    print("resend_email MCP server not found in config")
PY
        ok "MCP config updated"
        cleanup_temp_yaml_env
    else
        cleanup_temp_yaml_env
        warn "PyYAML could not be installed; please edit $HERMES_CONFIG manually"
    fi
fi

# Remove virtual environment
if [[ -d "$VENV_DIR" ]] && confirm "Remove Python virtual environment at $VENV_DIR?"; then
    rm -rf "$VENV_DIR"
    ok "Removed virtual environment"
fi

# Remove environment file
if [[ -f "$ENV_FILE" ]] && confirm "Remove $ENV_FILE (contains secrets)?"; then
    rm -f "$ENV_FILE"
    ok "Removed $ENV_FILE"
fi

if [[ -d "$ROOT_DIR/data" ]]; then
    ok "Preserved runtime data at $ROOT_DIR/data"
fi

# Remove Python cache files
if confirm "Remove __pycache__ directories?"; then
    find "$ROOT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    ok "Removed __pycache__ directories"
fi

ok "Uninstall complete."
