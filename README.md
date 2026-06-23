# Resend Hermes Bridge

Resend Hermes Bridge 接收 Resend 的 `email.received` Webhook，校验 Svix 签名，从 Resend 拉取完整邮件及附件，并让本地 Hermes 实例处理发往配置好的机器人地址（如 `bot@example.com`）的邮件。

桥接层负责所有外部投递。Hermes 决定应该做什么，但在处理机器人邮件时被要求不要直接发送 Telegram 消息或邮件。

## 流程

对于发往配置的机器人收件箱的邮件：

1. 通过 `hermes send` 向所有者展示原始 inbound 邮件。
2. 将相关附件下载到本地运行时存储。
3. 让 Hermes 决策并执行请求的助手任务。
4. 可选地通过 Resend 发送邮件回复。
5. 通知所有者最终结果以及任何已发邮件的预览。

未发往机器人收件箱的邮件仅向所有者展示。

## 功能

- Resend Webhook 签名验证。
- 本地 SQLite 持久化审计日志 `state.db`。
- 附件下载限制与本地附件历史。
- 通过 Resend 自动回复机器人邮件，并强制使用发件人域名。
- 本地已认证的 `/send` 接口，用于外发邮件。
- 标准输入输出 MCP 服务器 `resend_mcp_server.py`，暴露 `send_email` 工具。
- 手动外发邮件采用“先草稿再发送”的行为。
- 未完成 Webhook 处理的运行时清理与恢复。

Hermes 任务提示词保存在 `prompts/hermes_email_task.md`；如需更改助手行为，请修改该文件，而不是把长提示词嵌入 Python 代码。

运行时状态默认存储在仓库目录下：

- `state.db`
- `attachments/`
- `mcp_email_drafts.json`
- `mcp_email_drafts.json.lock`

这些文件已被 git 忽略。

## 运维

使用管理 CLI 查看本地状态：

```sh
python -m bridge_admin status
python -m bridge_admin failed --limit 20
python -m bridge_admin steps <email_id>
python -m bridge_admin drafts
```

## 环境要求

- Python 3.11 或更高版本
- 已验证的 Resend 发信域名
- Resend inbound Webhook
- 已运行且启用 API 服务器的 Hermes gateway
- 可将 Resend Webhook 转发到本桥接层的公网 HTTPS 路由

## 配置

复制示例环境文件并替换所有密钥：

```sh
cp .env.example .env
```

必填项：

```text
RESEND_API_KEY=...
RESEND_WEBHOOK_SECRET=...
RESEND_BRIDGE_SEND_SECRET=...
RESEND_DOMAIN=example.com
BOT_FROM_LOCAL=bot
OWNER_FROM_LOCAL=mail
AI_NAME=Hermes
```

桥接层发送密钥请自行生成：

```sh
openssl rand -hex 32
```

机器人地址由 `BOT_FROM_LOCAL` 和 `RESEND_DOMAIN` 组合而成。例如 `BOT_FROM_LOCAL=bot`、`RESEND_DOMAIN=example.com` 对应 `bot@example.com`。

## Hermes

Hermes 必须暴露本地 OpenAI 兼容 API 服务器。桥接层从 `HERMES_HOME/config.yaml` 读取这些值；除非在 `.env` 中覆盖，否则 `HERMES_HOME` 默认为 `~/.hermes`：

```yaml
API_SERVER_ENABLED: true
API_SERVER_HOST: 127.0.0.1
API_SERVER_PORT: 8642
API_SERVER_KEY: ...
```

MCP 服务器可按实际安装路径注册到 Hermes：

```yaml
mcp_servers:
  resend_email:
    command: "/path/to/python"
    args:
      - "/path/to/resend-hermes-bridge/resend_mcp_server.py"
    env:
      RESEND_BRIDGE_URL: "http://127.0.0.1:8765"
```

仅当 `hermes` CLI 不在 `PATH`、`~/.local/bin` 或 `/usr/local/bin` 中时，才需要设置 `HERMES_SEND_BIN`。

生成的回复/报告附件允许存放在 `GENERATED_ATTACHMENT_ROOTS`。若未设置，默认使用 `~/.hermes/cache`。

## 本地开发

安装依赖：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

本地运行桥接层：

```sh
uvicorn app:app --host 127.0.0.1 --port 8765
```

运行测试：

```sh
./scripts/test.sh
```

测试脚本默认使用 `.test-venv`，因此不会修改运行时 `.venv`。

## 部署

systemd 用户服务示例与部署清单请参见 [docs/deploy.md](docs/deploy.md)。

## 安全说明

- 服务请绑定在 `127.0.0.1`；通过反向代理暴露公网，且只转发 Resend Webhook 路径。
- `.env`、`state.db`、`attachments/` 及草稿文件请勿提交到 git。
- `/send` 需要 `Authorization: Bearer <RESEND_BRIDGE_SEND_SECRET>`。
- 手动发送需要先保存草稿并确认；没有已保存草稿的直接手动发送会被拒绝。
- 机器人自动回复的附件仅限同一封 inbound 邮件下载的文件。

## 许可证

MIT
