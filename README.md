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

这个项目不是托管邮件服务，也不是 Webmail。它默认绑定在 `127.0.0.1`，只建议通过反向代理暴露 Resend Webhook 路径。

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
Hermes MCP send_email
    |
    |-- confirmed=false: create local draft and show preview
    |-- user confirms in chat
    |-- confirmed=true + draft_id: bridge validates draft and sends
    v
Resend outbound email
```

## 快速安装

```sh
git clone https://github.com/AstraLeap/resend-hermes-bridge.git
cd resend-hermes-bridge
./scripts/install.sh
```

安装脚本会：

- 检查 Python 版本，要求 Python 3.11 或更高版本。
- 创建 `.venv` 并安装运行依赖。
- 从 `.env.example` 创建 `.env`，并交互式填写常用配置。
- 检查本机是否能找到 `hermes` CLI。
- 可选安装 systemd 用户服务。
- 可选把 MCP server 注册到 Hermes `config.yaml`。

如果安装时选择了 systemd 服务，可以启动：

```sh
systemctl --user enable --now resend-hermes-bridge.service
```

检查服务：

```sh
systemctl --user status resend-hermes-bridge.service
curl http://127.0.0.1:8765/health
```

卸载：

```sh
./scripts/uninstall.sh
```

卸载脚本会停止并删除 systemd 用户服务，并可选删除 MCP 配置、`.venv`、`.env` 和 Python 缓存。运行时数据 `data/` 默认保留。

## 手动安装

如果不使用安装脚本，可以手动执行：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env` 后直接运行：

```sh
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
```

或安装 systemd 用户服务。可以参考 `scripts/resend-hermes-bridge.service`：

```sh
mkdir -p ~/.config/systemd/user
cp scripts/resend-hermes-bridge.service ~/.config/systemd/user/resend-hermes-bridge.service
systemctl --user daemon-reload
systemctl --user enable --now resend-hermes-bridge.service
```

复制模板后必须把里面的 `/path/to/resend-hermes-bridge` 和用户路径改成实际路径。

## Resend 配置

在 Resend Dashboard 里需要准备：

1. 一个已验证的发信域名，例如 `example.com`。
2. 一个 API key，写入 `RESEND_API_KEY`。
3. 一个 Inbound Email Webhook，事件选择 `email.received`。
4. Webhook Signing Secret，写入 `RESEND_WEBHOOK_SECRET`。

公网入口只需要转发 Webhook 路径：

```text
https://your-domain.example/webhooks/resend
    -> http://127.0.0.1:8765/webhooks/resend
```

不要把 `/send`、`/show-draft`、`/health` 整个服务直接暴露到公网。

本地测试 Webhook 入队路径：

```sh
python scripts/send_test_webhook.py
```

默认会使用临时 `email_id`，适合测试签名和入队。要测试完整 Resend 拉取流程，设置一个真实的 inbound email ID：

```sh
RESEND_TEST_EMAIL_ID=re_... python scripts/send_test_webhook.py
```

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
| `BRIDGE_RETENTION_DAYS` | `90` | SQLite 历史清理保留天数。 |
| `BRIDGE_RECOVER_FAILED_EVENTS` | `true` | 服务启动时是否恢复未完成事件。 |
| `BRIDGE_EVENT_RECOVERY_LIMIT` | `50` | 单次恢复事件数量上限。 |
| `RESEND_MCP_DRAFT_TTL_SECONDS` | `604800` | MCP 邮件草稿保留时间。 |
| `BOT_REPLY_CONTEXT_TTL_SECONDS` | `600` | 机器人自动回复上下文有效期。 |

Hermes 各通知平台的凭证放在 Hermes 自己的配置里，通常是 `~/.hermes/.env`，不要放进本项目仓库。

## Hermes 依赖

桥接层会在本机调用 Hermes CLI。查找顺序包括：

- `PATH`
- `~/.local/bin/hermes`
- `~/.hermes/bin/hermes`
- `/usr/local/bin/hermes`

机器人邮件处理时调用：

```sh
hermes chat --query <prompt> --quiet --source tool --yolo
```

主人通知时调用：

```sh
hermes send --to <NOTIFICATION_TARGET> <message>
```

Telegram 文本通知会优先尝试 Rich Messages，失败后回退到 `hermes send`。附件通知使用 `MEDIA:<path>` 形式发送，并把通知文本和成功发送的附件写入 Hermes 会话上下文，方便你后续在聊天里引用“刚才那封邮件”或“刚才那个附件”。

## MCP 发信

安装 MCP 配置：

```sh
.venv/bin/python manage.py install-mcp
```

安装后 Hermes `config.yaml` 会增加：

```yaml
mcp_servers:
  resend_email:
    command: "/path/to/resend-hermes-bridge/.venv/bin/python"
    args:
      - "/path/to/resend-hermes-bridge/resend_mcp_server.py"
    env:
      RESEND_BRIDGE_URL: "http://127.0.0.1:8765"
```

MCP server 暴露 `send_email` 工具。它有一个强制草稿确认流程：

1. Hermes 首次调用 `send_email(..., confirmed=false)`。
2. MCP 在 `data/mcp_email_drafts.json` 创建草稿和隐藏确认 token。
3. 桥接层通过 `/show-draft` 给你发送标准草稿预览。
4. 你在聊天里确认。
5. Hermes 再调用 `send_email(draft_id=..., confirmed=true)`。
6. MCP 把草稿里的隐藏 token 交给 `/send`。
7. 桥接层在文件锁内验证 draft id、token、TTL、payload 一致性，并原子标记 `sending=true`。
8. 发送成功后标记 `sent=true`，同一个草稿不能重复发送。

注意：

- 没有草稿的手动外发会被拒绝。
- 确认发送时 payload 必须和草稿完全一致。
- 修改邮件内容会创建新草稿，旧草稿不会被原地改写。
- 草稿预览故意不展示附件内容，避免通知端过长或泄露本地路径细节。
- 升级本项目代码后，需要重启 Hermes/MCP 进程，否则 Hermes 可能仍在运行旧版 MCP server。

## 入站邮件流程

收到 Resend Webhook 后：

1. `/webhooks/resend` 使用 Svix 签名校验请求。
2. 事件写入 SQLite，重复事件会被识别为 duplicate。
3. 后台任务从 Resend 拉取完整邮件和附件列表。
4. 邮件和处理步骤写入 `data/state.db`。
5. 入站附件按大小限制下载到 `data/attachments/<email_id>/`；不再做“相关性”筛选。单个附件下载失败会记录错误，但不会阻断整封邮件继续处理。
6. 如果邮件没有发给机器人邮箱，只通知主人。
7. 如果邮件发给机器人邮箱，先把原始邮件展示给主人，再运行 Hermes 任务。
8. Hermes 返回严格 JSON 决策。
9. 如果决策是 `action=reply`，桥接层通过 Resend 回复发件人。
10. 无论是否回信，桥接层都会把 `owner_report` 发给主人。

Hermes 任务提示词在：

```text
prompts/hermes_email_task.md
```

要调整机器人处理邮件的行为，应优先改这个提示词，而不是把长提示词写进 Python 代码。

## 自动回复和附件边界

机器人自动回复使用 `BOT_FROM_LOCAL@RESEND_DOMAIN` 发信，只允许回复当前入站邮件的发件人。

自动回复附件只允许来自：

- 当前邮件下载到 `data/attachments/<email_id>/` 的附件。
- Hermes 生成到 `data/generated/` 的文件。

手动 MCP 发信可以附加本地路径或 base64 内容，但仍受附件数量和大小限制。

生成文件建议放在：

```text
data/generated/
```

该目录启动时会自动创建，并被 git 忽略。

## HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 本地健康检查。 |
| `POST` | `/webhooks/resend` | Resend Inbound Webhook。 |
| `POST` | `/show-draft` | 本地 MCP 草稿预览通知。 |
| `POST` | `/send` | 本地外发邮件接口。 |

安全边界：

- `/webhooks/resend` 面向公网时必须保留 Resend/Svix 签名校验。
- `/send` 和 `/show-draft` 是本机内部接口，不设计为公网 API。
- 服务推荐始终绑定 `127.0.0.1`。

## 运行时数据

运行时状态保存在仓库目录下的 `data/`：

```text
data/state.db
data/attachments/
data/generated/
data/bot_reply_contexts/
data/mcp_email_drafts.json
data/mcp_email_drafts.json.lock
```

这些文件包含邮件、附件、通知记录、草稿和发送审计，已经被 git 忽略。不要把它们提交到仓库。

启动时会加固运行时文件权限，并按 `BRIDGE_RETENTION_DAYS` 清理旧历史。

## 运维命令

查看数据库健康状态：

```sh
.venv/bin/python manage.py status
```

查看失败事件：

```sh
.venv/bin/python manage.py failed --limit 20
```

查看某封邮件的处理步骤：

```sh
.venv/bin/python manage.py steps <email_id>
```

查看 MCP 草稿：

```sh
.venv/bin/python manage.py drafts
```

重新注册 MCP：

```sh
.venv/bin/python manage.py install-mcp
```

查看服务日志：

```sh
journalctl --user -u resend-hermes-bridge.service -f
```

## 常见问题

### Webhook 返回 `invalid webhook signature`

检查 `RESEND_WEBHOOK_SECRET` 是否是 Resend 当前 Webhook 的 Signing Secret。更换 Webhook 或重新生成 secret 后，需要同步更新 `.env` 并重启服务。

### `/health` 里 `hermes_binary_available=false`

桥接服务找不到 Hermes CLI。确认 `hermes` 在 `PATH` 中，或位于 `~/.local/bin/hermes`、`~/.hermes/bin/hermes`、`/usr/local/bin/hermes` 之一。systemd 服务有自己的 `PATH`，修改 service 后要执行：

```sh
systemctl --user daemon-reload
systemctl --user restart resend-hermes-bridge.service
```

### MCP 创建了草稿，但确认后没有发送

先看草稿状态：

```sh
.venv/bin/python manage.py drafts
```

再看服务日志：

```sh
journalctl --user -u resend-hermes-bridge.service -n 100
```

常见原因：

- Hermes/MCP 进程还在运行旧代码。升级后需要重启 Hermes 或让 MCP server 重新启动。
- 草稿已过期，受 `RESEND_MCP_DRAFT_TTL_SECONDS` 控制。
- 确认时 payload 和原草稿不一致。
- Resend API 返回发信错误。

### 附件没有随通知或邮件发出

检查大小限制：

- 入站下载：`MAX_ATTACHMENT_DOWNLOAD_BYTES`
- 外发附件：`MAX_OUTBOUND_ATTACHMENT_BYTES`

自动回复附件还必须来自当前入站邮件附件目录或 `data/generated/`。

### 机器人没有自动回复，只通知了主人

Hermes 决策里只有 `action=reply` 且有可用 `reply_text` 时才会发邮件。否则桥接层会降级为通知主人，避免发出空回复。

## 本地开发

开发环境：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

运行服务：

```sh
uvicorn app:app --host 127.0.0.1 --port 8765
```

运行检查：

```sh
./scripts/test.sh
```

`scripts/test.sh` 默认使用 `.test-venv`，不会污染开发用的 `.venv`。检查包括 Ruff 和 pytest。

项目结构：

```text
app.py                         FastAPI app 创建、配置加载和共享编排函数
routers/                       HTTP 路由
services/                      Resend、Hermes、通知、入站邮件处理逻辑
db/                            SQLite schema 和读写操作
utils/                         邮件地址、展示格式等通用工具
resend_mcp_server.py           Hermes MCP stdio server
manage.py                      运维 CLI
prompts/hermes_email_task.md   机器人邮件任务提示词
scripts/                       安装、卸载、测试和本地 webhook 辅助脚本
tests/test_app.py              pytest 测试
```

## 安全注意

- `.env`、`data/`、附件、草稿和 SQLite 数据库都可能包含敏感信息，不要提交。
- 只把 `/webhooks/resend` 暴露给公网，且必须让签名校验继续生效。
- `/send` 和 `/show-draft` 没有设计成公网认证 API，只能放在本机或可信内网。
- Hermes 处理邮件时使用 `--yolo`。邮件正文和附件都是不可信输入，不要在高权限账户或不受控主机上运行。
- 不要让反向代理把整个 `127.0.0.1:8765` 暴露出去。

## 许可证

MIT
