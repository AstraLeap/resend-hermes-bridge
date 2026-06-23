from __future__ import annotations

import argparse
import json
from typing import Any

import app as bridge_app


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_status(_args: argparse.Namespace) -> None:
    print_json(bridge_app.db_health())


def command_failed(args: argparse.Namespace) -> None:
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
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
