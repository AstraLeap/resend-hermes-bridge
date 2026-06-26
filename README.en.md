# Resend Hermes Bridge

[中文](README.md) | English

Resend Hermes Bridge is a local FastAPI bridge service that connects Resend Inbound Email to a local Hermes runtime.

It does three things:

1. Receives and verifies Resend `email.received` webhooks.
2. Fetches the full inbound email and attachments, notifies the owner, and hands off bot-addressed mail to Hermes.
3. Handles all outbound email, including bot auto-replies and Hermes MCP manual sends.

Hermes decides what to do, but the bridge handles external delivery. This keeps webhook signature verification, attachment persistence, Resend sending, draft confirmation, audit logging, and recovery logic in one boundary.

## Use cases

- You already have Hermes installed and configured locally.
- You have a verified Resend sending domain.
- You want addresses like `bot@example.com` to become an email entry point for your local Hermes.
- You want Hermes to create email drafts via MCP, but require confirmation before anything is actually sent.
- You want local SQLite audit records for all emails, notifications, attachments, and processing steps.

This project is not a hosted email service or webmail. It binds to `127.0.0.1` by default and only the Resend webhook path should be exposed through a reverse proxy. The MCP server registered in Hermes is named `resend_email`.

## Architecture overview

```text
Resend Inbound
    |
    |  email.received + Svix signature
    v
FastAPI bridge 127.0.0.1:8765
    |
    |-- fetch inbound email and attachments from Resend
    |-- write state to data/state.db
    |-- notify owner through Hermes send
    |-- run Hermes chat for bot-addressed emails
    |-- send Resend replies when Hermes returns action=reply
    |
    v
Hermes local runtime
```

Manual sending follows a separate path:

```text
Hermes MCP resend_email
    |
    |-- confirmed=false: create local draft and show preview
    |-- user confirms in chat
    |-- confirmed=true + draft_id: bridge validates draft and sends
    |-- list/search/view/delete/tag local email history
    v
Resend outbound email
```

## Resend configuration

Set up the email side in Resend first:

1. Add and verify a sending domain on `resend.com`, for example `example.com`.
2. Create an API key; this will go into `RESEND_API_KEY`.
3. Create an Inbound Email webhook and select the `email.received` event.
4. Copy the webhook signing secret; this will go into `RESEND_WEBHOOK_SECRET`.

## Nginx forwarding

Enter your public endpoint in Resend Webhooks, then forward it on your server to the local fixed port:

```text
https://your-domain.example/your-resend-endpoint
    -> http://127.0.0.1:8765/webhooks/resend
```

If you changed `RESEND_BRIDGE_PORT` in `.env`, update `8765` in the mapping above accordingly.

Do not expose `/send`, `/show-draft`, or `/health` to the public internet.

## Quick install

```sh
git clone https://github.com/AstraLeap/resend-hermes-bridge.git
cd resend-hermes-bridge
./scripts/install.sh
```

Check the service:

```sh
systemctl --user status resend-hermes-bridge.service
curl http://127.0.0.1:8765/health
```

If you used a custom port, replace `8765` above with `RESEND_BRIDGE_PORT` from `.env`.

Uninstall:

```sh
./scripts/uninstall.sh
```

## Manual install

If you prefer not to use the install script, run manually:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env
```

After editing `.env`, install the systemd user service and MCP config:

```sh
mkdir -p ~/.config/systemd/user
cp scripts/resend-hermes-bridge.service ~/.config/systemd/user/resend-hermes-bridge.service
$EDITOR ~/.config/systemd/user/resend-hermes-bridge.service
systemctl --user daemon-reload
.venv/bin/python scripts/manage.py install-mcp
systemctl --user enable --now resend-hermes-bridge.service
```

Replace `/path/to/resend-hermes-bridge` and user paths in the template with actual values.

## Operations commands

Check database health:

```sh
.venv/bin/python scripts/manage.py status
```

View failed events:

```sh
.venv/bin/python scripts/manage.py failed --limit 20
```

View processing steps for one email:

```sh
.venv/bin/python scripts/manage.py steps <email_id>
```

View MCP drafts:

```sh
.venv/bin/python scripts/manage.py drafts
```

Re-register the MCP server:

```sh
.venv/bin/python scripts/manage.py install-mcp
```

View service logs:

```sh
journalctl --user -u resend-hermes-bridge.service -f
```

## Community

- [linux.do](https://linux.do/)

## License

MIT
