from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

import bridge_settings


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_status(_args: argparse.Namespace) -> None:
    import app as bridge_app

    print_json(bridge_app.db_health())


def command_failed(args: argparse.Namespace) -> None:
    import app as bridge_app

    with bridge_app.open_db() as conn:
        rows = list(
            conn.execute(
                """
                SELECT svix_id, email_id, status, updated_at, error
                FROM events
                WHERE status = 'failed'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (args.limit,),
            )
        )
    print_json([dict(row) for row in rows])


def command_steps(args: argparse.Namespace) -> None:
    import app as bridge_app

    with bridge_app.open_db() as conn:
        rows = list(
            conn.execute(
                """
                SELECT email_id, step, status, created_at, detail_json, error
                FROM processing_steps
                WHERE email_id = ?
                ORDER BY id ASC
                """,
                (args.email_id,),
            )
        )
    print_json([dict(row) for row in rows])


def command_drafts(_args: argparse.Namespace) -> None:
    import app as bridge_app

    if not bridge_app.SETTINGS.mcp_drafts_file.exists():
        print_json([])
        return
    try:
        data = json.loads(bridge_app.SETTINGS.mcp_drafts_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print_json({"error": "could not read draft file"})
        return
    drafts = []
    for draft_id, draft in data.items():
        if not isinstance(draft, dict):
            continue
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        drafts.append(
            {
                "draft_id": draft_id,
                "created_at": draft.get("created_at"),
                "sent": bool(draft.get("sent")),
                "sending": bool(draft.get("sending")),
                "to": payload.get("to", []),
                "subject": payload.get("subject", ""),
            }
        )
    print_json(drafts)


def command_install_mcp(_args: argparse.Namespace) -> None:
    """Register the Resend MCP server in Hermes config.yaml."""
    hermes_home = bridge_settings.hermes_home()
    config_path = hermes_home / "config.yaml"
    if not config_path.exists():
        raise RuntimeError(f"Hermes config not found at {config_path}")

    config_text = config_path.read_text(encoding="utf-8")
    config: dict[str, Any] = yaml.safe_load(config_text) or {}

    if "mcp_servers" not in config:
        config["mcp_servers"] = {}
    if not isinstance(config["mcp_servers"], dict):
        raise RuntimeError("Hermes config.yaml has an invalid mcp_servers value")

    repo_dir = Path(__file__).resolve().parent
    python_bin = Path(sys.executable).resolve()
    mcp_server_path = repo_dir / "resend_mcp_server.py"

    bridge_url = os.getenv("RESEND_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")

    config["mcp_servers"]["resend_email"] = {
        "command": str(python_bin),
        "args": [str(mcp_server_path)],
        "env": {"RESEND_BRIDGE_URL": bridge_url},
    }

    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print_json({
        "ok": True,
        "config_path": str(config_path),
        "server": "resend_email",
        "command": str(python_bin),
        "args": [str(mcp_server_path)],
        "env": {"RESEND_BRIDGE_URL": bridge_url},
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect resend-hermes-bridge state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="show database health")
    status.set_defaults(func=command_status)

    failed = subparsers.add_parser("failed", help="list failed webhook events")
    failed.add_argument("--limit", type=int, default=20)
    failed.set_defaults(func=command_failed)

    steps = subparsers.add_parser("steps", help="show processing steps for an email")
    steps.add_argument("email_id")
    steps.set_defaults(func=command_steps)

    drafts = subparsers.add_parser("drafts", help="list local MCP drafts")
    drafts.set_defaults(func=command_drafts)

    install_mcp = subparsers.add_parser(
        "install-mcp", help="register the Resend MCP server in Hermes config.yaml"
    )
    install_mcp.set_defaults(func=command_install_mcp)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
