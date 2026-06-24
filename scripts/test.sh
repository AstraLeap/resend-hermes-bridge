#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_VENV="${TEST_VENV:-$ROOT_DIR/.test-venv}"
TEST_PYTHON="${TEST_PYTHON:-}"
REQ_HASH_FILE="$TEST_VENV/.requirements.hash"
REQ_HASH="$(
  python3 - "$ROOT_DIR/requirements.txt" "$ROOT_DIR/requirements-dev.txt" <<'PY'
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

digest = hashlib.sha256()
for path in map(Path, sys.argv[1:]):
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
)"

has_pytest() {
  "$1" - <<'PY' >/dev/null 2>&1
import pytest
PY
}

install_test_env() {
  local python_bin="$1"
  "$python_bin" -m pip install -r "$ROOT_DIR/requirements-dev.txt" >&2
  mkdir -p "$(dirname "$REQ_HASH_FILE")"
  printf '%s\n' "$REQ_HASH" > "$REQ_HASH_FILE"
}

ensure_test_venv() {
  local python_bin="$TEST_VENV/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    python3 -m venv "$TEST_VENV"
  fi
  if [[ ! -f "$REQ_HASH_FILE" ]] || [[ "$(cat "$REQ_HASH_FILE")" != "$REQ_HASH" ]] || ! has_pytest "$python_bin"; then
    install_test_env "$python_bin"
  fi
  printf '%s\n' "$python_bin"
}

if [[ -n "$TEST_PYTHON" ]]; then
  PYTHON_BIN="$TEST_PYTHON"
  if ! has_pytest "$PYTHON_BIN"; then
    install_test_env "$PYTHON_BIN"
  fi
else
  PYTHON_BIN="$(ensure_test_venv)"
fi

if [[ $# -eq 0 ]]; then
  set -- -q "$ROOT_DIR/tests/test_app.py"
fi

export RESEND_API_KEY=test
export RESEND_WEBHOOK_SECRET=test
export RESEND_DOMAIN=example.com
export BOT_FROM_LOCAL=bot
export OWNER_FROM_LOCAL=mail

"$PYTHON_BIN" -m ruff check "$ROOT_DIR"
exec "$PYTHON_BIN" -m pytest "$@"
