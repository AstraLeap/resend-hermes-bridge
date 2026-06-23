# Deployment

This guide assumes a Linux host running systemd user services.

## 1. Clone and install

```sh
git clone https://github.com/your-org/resend-hermes-bridge.git
cd resend-hermes-bridge
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure environment

```sh
cp .env.example .env
$EDITOR .env
```

Set at least:

```text
RESEND_API_KEY=...
RESEND_WEBHOOK_SECRET=...
RESEND_BRIDGE_SEND_SECRET=...
RESEND_DOMAIN=example.com
BOT_FROM_LOCAL=bot
OWNER_FROM_LOCAL=mail
AI_NAME=Hermes
```

Generate `RESEND_BRIDGE_SEND_SECRET` with:

```sh
openssl rand -hex 32
```

## 3. Configure Hermes

Enable the Hermes API server in `HERMES_HOME/config.yaml`. If you do not set
`HERMES_HOME` in `.env`, the bridge uses `~/.hermes`:

```yaml
API_SERVER_ENABLED: true
API_SERVER_HOST: 127.0.0.1
API_SERVER_PORT: 8642
API_SERVER_KEY: ...
```

Register the MCP server with paths for your machine:

```yaml
mcp_servers:
  resend_email:
    command: "/home/your-user/resend-hermes-bridge/.venv/bin/python"
    args:
      - "/home/your-user/resend-hermes-bridge/resend_mcp_server.py"
    env:
      RESEND_BRIDGE_URL: "http://127.0.0.1:8765"
```

Only set path overrides such as `HERMES_HOME`, `HERMES_SEND_BIN`, and
`GENERATED_ATTACHMENT_ROOTS` when your Hermes installation does not use the
default home-directory layout.

## 4. Install the systemd user service

Copy the example unit and edit the paths if your clone is not at
`$HOME/resend-hermes-bridge`:

```sh
mkdir -p ~/.config/systemd/user
cp docs/resend-hermes-bridge.service.example \
  ~/.config/systemd/user/resend-hermes-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now resend-hermes-bridge.service
```

Check status and logs:

```sh
systemctl --user status resend-hermes-bridge.service
journalctl --user -u resend-hermes-bridge.service -f
```

## 5. Expose the webhook

Keep Uvicorn bound to `127.0.0.1:8765`. Use your reverse proxy to forward only
the webhook route to the bridge. The public URL you configure in Resend should
point to:

```text
https://your-domain.example/api/resend/inbound
```

Configure your proxy to route that path to:

```text
http://127.0.0.1:8765/webhooks/resend
```

The exact proxy syntax depends on your web server or platform.

## 6. Upgrade

```sh
git pull
. .venv/bin/activate
pip install -r requirements.txt
systemctl --user restart resend-hermes-bridge.service
```

Restart Hermes as well if you change the MCP server configuration.
