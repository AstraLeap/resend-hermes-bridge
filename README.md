# Resend Hermes Bridge

Resend Hermes Bridge 是一个运行在本机的 FastAPI 桥接服务，用来把 Resend Inbound Email 接入本地 Hermes 运行时。

它做三件事：

1. 接收并校验 Resend 的 `email.received` Webhook。
2. 拉取完整入站邮件和附件，通知邮件主人，并在命中机器人邮箱时交给 Hermes 处理。
3. 统一负责对外发邮件，包括机器人自动回复和 Hermes MCP 手动发信。

Hermes 可以决定任务怎么做，但外部发送动作由桥接层落地。这样可以把 Webhook 验签、附件落盘、Resend 发信、草稿确认、审计日志和恢复逻辑集中在一个边界里。

## 适用场景

- 你已经在本机安装并配置了 Hermes。
- 你有一个已验证的 Resend 发信域名。
- 你希望把 `bot@example.com` 这类邮箱当成本地 Hermes 的邮件入口。
- 你希望 Hermes 能通过 MCP 创建邮件草稿，但真正发送前必须展示给你确认。
- 你希望所有邮件、通知、附件和处理步骤在本机有 SQLite 审计记录。

这个项目不是托管邮件服务，也不是 Webmail。它默认绑定在 `127.0.0.1`，只建议通过反向代理暴露 Resend Webhook 路径。Hermes 里注册的 MCP 名称是 `resend_email`。

## 架构概览

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

手动发信走另一条路径：

```text
Hermes MCP resend_email
    |
    |-- confirmed=false: create local draft and show preview
    |-- user confirms in chat
    |-- confirmed=true + draft_id: bridge validates draft and sends
    |-- search/view/delete/tag local email history
    v
Resend outbound email
```

## Resend 配置

先在 Resend 配好邮件侧资源：

1. 在 `resend.com` 添加并验证发信域名，例如 `example.com`。
2. 创建 API key，后续写入 `RESEND_API_KEY`。
3. 创建 Inbound Email Webhook，事件选择 `email.received`。
4. 复制 Webhook Signing Secret，后续写入 `RESEND_WEBHOOK_SECRET`。

## Nginx 转发

在 Resend Webhooks 里填写你的公网 Endpoint，服务器上用 Nginx 转发到本地固定接口：

```text
https://your-domain.example/your-resend-endpoint
    -> http://127.0.0.1:8765/webhooks/resend
```

示例：

```nginx
location /your-resend-endpoint {
    proxy_pass http://127.0.0.1:8765/webhooks/resend;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

不要把 `/send`、`/show-draft`、`/health` 整个服务直接暴露到公网。

## 快速安装

```sh
git clone https://github.com/AstraLeap/resend-hermes-bridge.git
cd resend-hermes-bridge
./scripts/install.sh
```

安装脚本会：

- 检查 Hermes CLI 和 `~/.hermes/config.yaml`，未配置 Hermes 会直接退出。
- 检查 systemd，服务只按 systemd 用户服务安装。
- 检查 Python 版本，要求 Python 3.11 或更高版本。
- 创建 `.venv` 并安装运行依赖。
- 从 `.env.example` 创建 `.env`，并交互式填写常用配置。
- 安装 systemd 用户服务。
- 把 MCP server 注册到 Hermes `config.yaml`。

启动服务：

```sh
systemctl --user enable --now resend-hermes-bridge.service
```

检查服务：

```sh
systemctl --user status resend-hermes-bridge.service
curl http://127.0.0.1:8765/health
```

如果 Hermes 会话已经打开，执行 `/reload-mcp` 重新加载 MCP 工具。

卸载：

```sh
./scripts/uninstall.sh
```

卸载脚本会停止并删除 systemd 用户服务，并可选删除 MCP 配置、`.venv`、`.env` 和 Python 缓存。运行时数据 `data/` 默认保留。

## 配置项

核心地址由两个 local part 和一个域名组成：

```text
BOT_FROM_LOCAL=bot
OWNER_FROM_LOCAL=mail
RESEND_DOMAIN=example.com
```

对应地址：

- 机器人邮箱：`bot@example.com`
- 主人/手动发信邮箱：`mail@example.com`

常用配置：

| 变量 | 说明 |
| --- | --- |
| `RESEND_API_KEY` | Resend API key。 |
| `RESEND_WEBHOOK_SECRET` | Resend Inbound Webhook 的 Svix 签名密钥。 |
| `RESEND_DOMAIN` | 已验证发信域名，不带 `@`。 |
| `BOT_FROM_LOCAL` | 机器人邮箱 local part。 |
| `OWNER_FROM_LOCAL` | 主人/手动外发邮箱 local part。 |
| `AI_NAME` | 给主人通知时显示的助手名称。 |
| `NOTIFICATION_TARGET` | 传给 `hermes send --to` 的目标，例如 `telegram`、`weixin`、`qqbot`、`wecom`、`discord`、`slack`、`signal`。 |
| `RESEND_BRIDGE_URL` | MCP server 访问桥接服务的 URL，默认 `http://127.0.0.1:8765`。 |
| `LOG_LEVEL` | Python 日志等级，默认 `INFO`。 |

运行限制和保留策略：

| 变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `MAX_ATTACHMENT_DOWNLOAD_BYTES` | `15728640` | 单个入站附件下载上限。 |
| `MAX_OUTBOUND_ATTACHMENT_BYTES` | `31457280` | 单个外发附件和外发附件总量上限。 |
| `HERMES_TIMEOUT_SECONDS` | `180` | Hermes 处理机器人邮件的超时时间。 |
| `HERMES_EMAIL_TASK_TOOLSETS` | `browser,clarify,code_execution,cronjob,delegation,file,image_gen,memory,session_search,skills,terminal,vision,web` | 机器人邮件子任务允许使用的 Hermes 工具集。会过滤掉 `resend_email`。 |
| `BRIDGE_RETENTION_DAYS` | `90` | SQLite 历史清理保留天数。 |
| `BRIDGE_RECOVER_FAILED_EVENTS` | `true` | 服务启动时是否恢复未完成事件。 |
| `BRIDGE_EVENT_RECOVERY_LIMIT` | `50` | 单次恢复事件数量上限。 |
| `RESEND_MCP_DRAFT_TTL_SECONDS` | `604800` | MCP 邮件草稿保留时间。 |
| `BOT_REPLY_CONTEXT_TTL_SECONDS` | `600` | 机器人自动回复上下文有效期。 |

Hermes 各通知平台的凭证放在 Hermes 自己的配置里，通常是 `~/.hermes/.env`，不要放进本项目仓库。

## 手动安装

如果不使用安装脚本，可以手动执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env` 后安装 systemd 用户服务和 MCP 配置：

```sh
mkdir -p ~/.config/systemd/user
cp scripts/resend-hermes-bridge.service ~/.config/systemd/user/resend-hermes-bridge.service
$EDITOR ~/.config/systemd/user/resend-hermes-bridge.service
systemctl --user daemon-reload
.venv/bin/python scripts/manage.py install-mcp
systemctl --user enable --now resend-hermes-bridge.service
```

模板里的 `/path/to/resend-hermes-bridge` 和用户路径必须改成实际路径。

## 运维命令

查看数据库健康状态：

```sh
.venv/bin/python scripts/manage.py status
```

查看失败事件：

```sh
.venv/bin/python scripts/manage.py failed --limit 20
```

查看某封邮件的处理步骤：

```sh
.venv/bin/python scripts/manage.py steps <email_id>
```

查看 MCP 草稿：

```sh
.venv/bin/python scripts/manage.py drafts
```

重新注册 MCP：

```sh
.venv/bin/python scripts/manage.py install-mcp
```

查看服务日志：

```sh
journalctl --user -u resend-hermes-bridge.service -f
```

## 安全注意

- `.env` 和 `data/` 都可能包含敏感信息，不要提交。
- 公网 Webhook 入口只转发到本地 `/webhooks/resend`，且必须让签名校验继续生效。
- `/send` 和 `/show-draft` 没有设计成公网认证 API，只能放在本机或可信内网。
- Hermes 处理邮件时使用 `--yolo`。邮件正文和附件都是不可信输入，不要在高权限账户或不受控主机上运行。
- 不要让反向代理把整个 `127.0.0.1:8765` 暴露出去。

## 许可证

MIT
