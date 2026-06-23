# Resend Hermes Bridge

Resend Hermes Bridge 接收 Resend 的 `email.received` Webhook，校验 Svix 签名，从 Resend 拉取完整邮件及附件，并让本地 Hermes 实例处理发往配置好的机器人地址（如 `bot@example.com`）的邮件。

桥接层负责所有外部投递。Hermes 决定应该做什么，但在处理机器人邮件时被要求不要直接发送 Telegram 消息或邮件。

## 快速开始

```sh
git clone https://github.com/AstraLeap/resend-hermes-bridge.git
cd resend-hermes-bridge
./scripts/setup.sh          # 交互式填写配置、生成密钥、可选安装 systemd/MCP

systemctl --user enable --now hermes-send-proxy.service
docker compose up -d --build
```

测试本地链路（无需真实邮件）：

```sh
python scripts/send_test_webhook.py
```

最后把 Resend inbound webhook 指向你的公网入口，反向代理再转发到 `http://127.0.0.1:8765/webhooks/resend`。

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
- 已安装并配置好消息平台的宿主机 Hermes
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

通知渠道通过 `NOTIFICATION_TARGET` 设置，例如 `telegram`、`weixin`、`qqbot`、`wecom`、`discord`、`slack`、`signal`。对应平台的凭证在 Hermes（`~/.hermes/.env`）中配置，桥接层只负责把消息交给 `hermes send --to <target>`。

Hermes 代理默认监听 `http://127.0.0.1:18765`。只需要在 `HERMES_PROXY_URL` 中记录这个 base URL，桥接层会从它派生 `/task` 和 `/send`。

桥接层发送密钥请自行生成：

```sh
openssl rand -hex 32
```

机器人地址由 `BOT_FROM_LOCAL` 和 `RESEND_DOMAIN` 组合而成。例如 `BOT_FROM_LOCAL=bot`、`RESEND_DOMAIN=example.com` 对应 `bot@example.com`。

## Hermes

默认 Docker 部署下，桥接层不会直接在容器里运行真实 Hermes，也不会直接调用 Telegram/QQBot API。容器通过宿主机上的代理服务调用：

- `/task`：启动新的宿主机 Hermes Agent CLI 子会话处理邮件任务，并要求最终只返回 JSON。
- `/send`：调用宿主机 `hermes send` 给 Telegram、QQBot 等渠道发消息；多媒体附件使用 `MEDIA:<path>`。

Docker Compose 默认使用 host network，因此容器内访问 `127.0.0.1:18765` 时会连接到宿主机 Hermes 代理。桥接服务把 `./data` 映射到容器 `/app/data`，并把宿主机 Hermes cache 映射到 `/root/.hermes/cache`。

桥接层不会把 bridge 自己的 `data/` 路径传给 Hermes。入站附件下载后会复制到 Hermes cache 下的 `resend-bridge/inbound/<email_id>/`，提示 Hermes 生成的文件保存到 `resend-bridge/generated/`。Hermes 对输入/输出附件的读写都发生在 `~/.hermes/cache/resend-bridge/` 子目录内。

如果 Hermes 也部署在 Docker 中，前提是 Hermes 容器和桥接容器共享同一份 Hermes cache bind mount 或命名卷。两个容器可以把这份 cache 挂到相同路径；如果 Hermes 运行时看到的 `~/.hermes` 路径不同，用 `HERMES_HOST_HOME` 告诉 bridge 如何把容器内路径翻译成 Hermes 运行时可见路径。

MCP 服务器可自动注册到 Hermes：

```sh
python -m bridge_admin install-mcp
```

或手动在 `HERMES_HOME/config.yaml` 中添加：

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

生成的回复/报告附件允许存放在 `GENERATED_ATTACHMENT_ROOTS`。Docker 默认使用 `/root/.hermes/cache/resend-bridge/generated`，并通过 `HERMES_HOST_HOME` 映射到 Hermes 运行时的 cache 路径。

## 本地开发

安装宿主机辅助依赖。`.venv` 只用于 MCP server 和 Hermes host proxy，不用于运行桥接 Web 服务：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

运行测试：

```sh
./scripts/test.sh
```

测试脚本默认使用 `.test-venv`，因此不会修改 host proxy/MCP 使用的 `.venv`。

## 部署

```sh
systemctl --user enable --now hermes-send-proxy.service
docker compose up -d --build
```

反向代理只需要把公网 Resend webhook 路由转发到
`http://127.0.0.1:8765/webhooks/resend`。

## 安全说明

- 服务请绑定在 `127.0.0.1`；通过反向代理暴露公网，且只转发 Resend Webhook 路径。
- `.env`、`state.db`、`attachments/` 及草稿文件请勿提交到 git。
- `/send` 需要 `Authorization: Bearer <RESEND_BRIDGE_SEND_SECRET>`。
- 手动发送需要先保存草稿并确认；没有已保存草稿的直接手动发送会被拒绝。
- 机器人自动回复的附件仅限同一封 inbound 邮件下载的文件。

## 许可证

MIT
