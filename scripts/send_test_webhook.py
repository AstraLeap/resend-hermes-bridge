#!/usr/bin/env python3
"""Send a fake Resend email.received webhook to the local bridge for testing."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from svix.webhooks import Webhook


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key not in os.environ:
            os.environ[key] = value


def generate_svix_headers(secret: str, payload: bytes) -> dict[str, str]:
    webhook_id = secrets.token_hex(16)
    timestamp = datetime.now(UTC)
    signature = Webhook(secret).sign(
        msg_id=webhook_id,
        timestamp=timestamp,
        data=payload.decode(),
    )
    return {
        "svix-id": webhook_id,
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": signature,
    }


def main() -> None:
    repo_dir = Path(__file__).resolve().parent.parent
    load_env_file(repo_dir / ".env")

    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
    if not secret:
        raise RuntimeError("RESEND_WEBHOOK_SECRET is not set in .env")

    bridge_url = os.environ.get("RESEND_BRIDGE_URL", "http://127.0.0.1:8765")
    endpoint = urljoin(bridge_url.rstrip("/") + "/", "webhooks/resend")

    domain = os.environ.get("RESEND_DOMAIN", "example.com")
    bot_local = os.environ.get("BOT_FROM_LOCAL", "bot")
    to_address = f"{bot_local}@{domain}"
    from_address = "tester@example.com"
    email_id = os.environ.get("RESEND_TEST_EMAIL_ID", f"test-{secrets.token_hex(8)}")

    payload = {
        "type": "email.received",
        "created_at": datetime.now(UTC).isoformat(),
        "data": {
            "email_id": email_id,
            "from": from_address,
            "to": [to_address],
            "subject": "Test webhook from resend-hermes-bridge",
            "text": "This is a test inbound email. If you see this in your notification channel, the bridge is working.",
            "html": "<p>This is a test inbound email.</p>",
        },
    }

    body = json.dumps(payload).encode()
    headers = generate_svix_headers(secret, body)
    headers["content-type"] = "application/json"

    print(f"POST {endpoint}")
    response = httpx.post(endpoint, content=body, headers=headers, timeout=30)
    print(f"Status: {response.status_code}")
    try:
        print(response.json())
    except Exception:
        print(response.text)


if __name__ == "__main__":
    main()
