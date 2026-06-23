# Resend Hermes Bridge

Resend Hermes Bridge receives Resend `email.received` webhooks, verifies the
Svix signature, fetches the full email plus attachments from Resend, and lets a
local Hermes instance process mail addressed to a configured bot address such as
`bot@example.com`.

The bridge owns all external delivery. Hermes decides what should happen, but it
is instructed not to send Telegram messages or email directly during bot-mail
processing.

## Flow

For mail addressed to the configured bot inbox:

1. Show the original inbound email to the owner through `hermes send`.
2. Download relevant attachments into local runtime storage.
3. Ask Hermes to decide and execute the requested assistant task.
4. Optionally send an email reply through Resend.
5. Notify the owner with the final result and any sent-message preview.

Mail not addressed to the bot inbox is only shown to the owner.

## Features

- Resend webhook signature verification.
- Durable local SQLite audit log in `state.db`.
- Attachment download limits and local attachment history.
- Bot auto-replies through Resend with sender-domain enforcement.
- A local authenticated `/send` endpoint for outbound email.
- A stdio MCP server, `resend_mcp_server.py`, exposing a `send_email` tool.
- Draft-before-send behavior for manual outbound email.
- Runtime pruning and recovery for incomplete webhook processing.

The Hermes task prompt is stored in `prompts/hermes_email_task.md`; update that
file when changing assistant behavior instead of embedding long prompt text in
Python code.

Runtime state is stored in the repository directory by default:

- `state.db`
- `attachments/`
- `mcp_email_drafts.json`
- `mcp_email_drafts.json.lock`

These files are ignored by git.

## Operations

Inspect local state with the admin CLI:

```sh
python -m bridge_admin status
python -m bridge_admin failed --limit 20
python -m bridge_admin steps <email_id>
python -m bridge_admin drafts
```

## Requirements

- Python 3.11 or newer
- A verified Resend sending domain
- A Resend inbound webhook
- A running Hermes gateway with the API server enabled
- A reachable public HTTPS route forwarding Resend webhooks to this bridge

## Configuration

Copy the example environment and replace every secret:

```sh
cp .env.example .env
```

Required values:

```text
RESEND_API_KEY=...
RESEND_WEBHOOK_SECRET=...
RESEND_BRIDGE_SEND_SECRET=...
RESEND_DOMAIN=example.com
BOT_FROM_LOCAL=bot
OWNER_FROM_LOCAL=mail
AI_NAME=Hermes
```

Generate the bridge send secret yourself:

```sh
openssl rand -hex 32
```

Your bot address is derived from `BOT_FROM_LOCAL` and `RESEND_DOMAIN`. For
example, `BOT_FROM_LOCAL=bot` and `RESEND_DOMAIN=example.com` means
`bot@example.com`.

## Hermes

Hermes must expose its local OpenAI-compatible API server. The bridge reads
these values from `HERMES_HOME/config.yaml`; `HERMES_HOME` defaults to
`~/.hermes` unless you override it in `.env`:

```yaml
API_SERVER_ENABLED: true
API_SERVER_HOST: 127.0.0.1
API_SERVER_PORT: 8642
API_SERVER_KEY: ...
```

The MCP server can be registered with Hermes using paths appropriate for your
installation:

```yaml
mcp_servers:
  resend_email:
    command: "/path/to/python"
    args:
      - "/path/to/resend-hermes-bridge/resend_mcp_server.py"
    env:
      RESEND_BRIDGE_URL: "http://127.0.0.1:8765"
```

Only set `HERMES_SEND_BIN` when the `hermes` CLI is not on `PATH`,
`~/.local/bin`, or `/usr/local/bin`.

Generated reply/report attachments are allowed from `GENERATED_ATTACHMENT_ROOTS`.
If unset, it defaults to `~/.hermes/cache`.

## Local Development

Install dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Run the bridge locally:

```sh
uvicorn app:app --host 127.0.0.1 --port 8765
```

Run tests:

```sh
./scripts/test.sh
```

The test script uses `.test-venv` by default so it does not modify the runtime
`.venv`.

## Deployment

See [docs/deploy.md](docs/deploy.md) for a systemd user-service example and
deployment checklist.

## Security Notes

- Keep the service bound to `127.0.0.1`; expose it publicly through a reverse
  proxy route that only forwards the Resend webhook path.
- Keep `.env`, `state.db`, `attachments/`, and draft files out of git.
- `/send` requires `Authorization: Bearer <RESEND_BRIDGE_SEND_SECRET>`.
- Manual sends require draft confirmation; direct manual sends without a saved
  draft are rejected.
- Bot auto-reply attachments are restricted to files downloaded for the same
  inbound email.

## License

MIT
