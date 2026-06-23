import asyncio
import json
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app
import manage


class _DummyFastMCP:
    def __init__(self, *_args, **_kwargs):
        pass

    def tool(self):
        def decorator(func):
            return func

        return decorator

    def run(self):
        pass


_mcp_module = types.ModuleType("mcp")
_mcp_server_module = types.ModuleType("mcp.server")
_mcp_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
_mcp_fastmcp_module.FastMCP = _DummyFastMCP
sys.modules.setdefault("mcp", _mcp_module)
sys.modules.setdefault("mcp.server", _mcp_server_module)
sys.modules.setdefault("mcp.server.fastmcp", _mcp_fastmcp_module)

import resend_mcp_server  # noqa: E402
import utils.notices as notices  # noqa: E402


def test_parse_loose_reply_decision_with_unescaped_quotes():
    content = (
        '{"action": "reply", '
        '"owner_report": "已回复测试邮件"回复内容是一只小狗"至 user@example.com", '
        '"reply_subject": "Re: 这个用于测试需要回复的邮件", "reply_text": "一只小狗"}'
    )

    decision = app.parse_json_decision(content)

    assert decision["action"] == "reply"
    assert decision["reply_subject"] == "Re: 这个用于测试需要回复的邮件"
    assert decision["reply_text"] == "一只小狗"


def test_bot_auto_reply_uses_inbound_sender_address():
    payload = app.build_resend_reply_payload(
        {
            "from": "user@example.com",
            "subject": "这个用于测试需要回复的邮件",
            "message_id": "<message@example.com>",
            "headers": {},
        },
        {
            "action": "reply",
            "reply_subject": "Re: 这个用于测试需要回复的邮件",
            "reply_text": "一只小狗",
        },
    )

    assert payload["from_local"] == "bot"
    assert payload["to"] == ["user@example.com"]
    assert payload["text"] == "一只小狗"


def test_parse_task_decision_preserves_execution_fields():
    decision = app.parse_json_decision(
        '{"action":"notify","executed_task":true,'
        '"owner_report":"北京今天多云，适合带伞。"}'
    )

    assert decision["action"] == "notify"
    assert decision["executed_task"] is True
    assert decision["owner_report"] == "北京今天多云，适合带伞。"


def test_exception_message_falls_back_to_exception_type():
    assert app.exception_message(TimeoutError()) == "TimeoutError"
    assert app.exception_message(RuntimeError("boom")) == "boom"


def test_reply_payload_can_fall_back_to_owner_report():
    payload = app.build_resend_reply_payload(
        {
            "from": "sender@example.com",
            "subject": "测试",
            "message_id": "<message@example.com>",
            "headers": {},
        },
        {
            "action": "reply",
            "owner_report": "任务已完成",
        },
    )

    assert payload["text"] == "任务已完成"


def test_activity_summary_mentions_execution_and_optional_reply():
    summary = app.build_activity_summary(
        {"from": "sender@example.com", "subject": "天气"},
        {
            "executed_task": True,
            "owner_report": "北京今天晴。",
        },
    )

    assert "**任务总结：**" in summary
    assert "北京今天晴" in summary
    assert "已收到并展示" not in summary
    assert "Hermes 判断" not in summary
    assert "未向发件人回邮件" not in summary
    assert "简要原因" not in summary


def test_non_bot_email_notice_uses_owner_title_without_routing_labels(monkeypatch):
    messages = []
    statuses = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        messages.append(message)

    def fake_update_status(email_id, status, error=None):
        statuses.append((email_id, status, error))

    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(app, "update_inbound_status", fake_update_status)

    asyncio.run(
        app.notify_non_bot_email(
            "email-1",
            {
                "id": "email-1",
                "from": "sender@example.com",
                "to": ["owner@example.com"],
                "subject": "Hello",
                "text": "Body",
            },
            [],
        )
    )

    assert messages
    assert "收到非 bot 邮件" not in messages[0]
    assert "邮件不是发给" not in messages[0]
    assert messages[0].startswith("主人你有一封新邮件~")
    assert statuses == [("email-1", "notified", None)]


def test_bot_email_notice_uses_kabao_title(monkeypatch):
    messages = []
    statuses = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        messages.append(message)

    def fake_update_status(email_id, status, error=None):
        statuses.append((email_id, status, error))

    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(app, "update_inbound_status", fake_update_status)

    asyncio.run(
        app.notify_bot_email_received(
            "email-1",
            {
                "id": "email-1",
                "from": "sender@example.com",
                "to": ["bot@example.com"],
                "subject": "Hello",
                "text": "Body",
            },
            [],
        )
    )

    assert messages
    assert messages[0].startswith("卡宝收到邮件啦！正在处理中哦~")
    assert "收到发给" not in messages[0]
    assert statuses == [("email-1", "processing", None)]


def test_bot_email_notice_ignores_custom_title_env(monkeypatch):
    messages = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        messages.append(message)

    monkeypatch.setenv("NOTIFICATION_BOT_TITLE", "不应该生效")
    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(app, "update_inbound_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, ai_name="Hermes"))

    asyncio.run(
        app.notify_bot_email_received(
            "email-1",
            {
                "id": "email-1",
                "from": "sender@example.com",
                "to": ["bot@example.com"],
                "subject": "Hello",
                "text": "Body",
            },
            [],
        )
    )

    assert messages[0].startswith("Hermes收到邮件啦！正在处理中哦~")
    assert "不应该生效" not in messages[0]


def test_notify_telegram_uses_hermes_send_by_default(monkeypatch, tmp_path):
    commands = []
    updates = []
    steps = []
    message = "| 字段 | 内容 |\n| --- | --- |\n| From | sender@example.com |"
    hermes_bin = tmp_path / "hermes"
    hermes_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes_bin.chmod(0o755)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"sent\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append(args)
        return FakeProcess()

    monkeypatch.setattr(
        app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, notification_target="telegram", hermes_send_bin=hermes_bin),
    )
    monkeypatch.setattr(app, "create_outbound_message", lambda **_kwargs: 99)
    monkeypatch.setattr(
        app,
        "update_outbound_message",
        lambda outbound_id, **kwargs: updates.append((outbound_id, kwargs)),
    )
    monkeypatch.setattr(
        app,
        "record_processing_step",
        lambda **kwargs: steps.append(kwargs),
    )

    asyncio.run(app.notify_telegram(message, email_id="email-1"))

    assert commands == [(str(hermes_bin), "send", "--to", "telegram", message)]
    assert updates[-1][0] == 99
    assert updates[-1][1]["status"] == app.OutboundStatus.SENT
    assert steps[-1]["step"] == "telegram_notify"


def test_reply_notice_shows_only_sent_email_without_summary_footer():
    notice = notices.render_processing_result_notice(
        "已通过 Resend 自动回复。",
        {},
        domain="example.com",
        reply_payload={
            "from_local": "bot",
            "to": ["sender@example.com"],
            "subject": "Re: 测试",
            "text": "一只小狗",
        },
        reply_id="reply-123",
    )

    assert "Hermes 已自动回复：" in notice
    assert "处理结果" not in notice
    assert "决策原因" not in notice
    assert "Hermes 汇报" not in notice
    assert "Reply ID" not in notice
    assert "一只小狗" in notice


def test_hermes_task_prompt_requires_owner_report_even_with_reply():
    prompt = app.build_hermes_task_prompt(
        {
            "task": "test",
            "sender": "sender@example.com",
            "email": {"text_preview": "请查天气并回我"},
            "attachments": [],
            "downloaded_files": [],
        }
    )

    assert "无论是否还要给发件人回邮件，都必须填写 owner_report" in prompt
    assert "- owner_report: 必填" in prompt
    assert "邮件主人" in prompt
    assert "给主人看" in prompt


def test_hermes_task_prompt_is_loaded_from_template():
    prompt = app.build_hermes_task_prompt(
        {
            "task": "test",
            "sender": "sender@example.com",
            "email": {"text_preview": "hello"},
            "attachments": [],
            "downloaded_files": [],
        }
    )

    assert "返回严格 JSON" in prompt
    assert "{prompt_record_json}" not in prompt


def test_hermes_task_prompt_keeps_bridge_as_delivery_owner():
    prompt = app.build_hermes_task_prompt(
        {
            "task": "test",
            "sender": "sender@example.com",
            "email": {"text_preview": "查询北京天气发给所有者"},
            "attachments": [],
            "downloaded_files": [],
        }
    )

    assert "不要自己调用 send_email" in prompt
    assert "owner_report" in prompt
    assert "第一人称“我/我们/我的”默认指 `sender`" in prompt
    assert "设置 action=reply" in prompt
    assert "forward_received_attachments" not in prompt
    assert "通知端（如 Telegram）" in prompt
    assert "不要编造不存在的路径" in prompt

def test_load_settings_uses_bridge_data_dir_env(monkeypatch, tmp_path):
    hermes_bin = tmp_path / "hermes-bin"
    hermes_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes_bin.chmod(0o755)
    data_dir = tmp_path / "runtime-data"

    monkeypatch.setenv("RESEND_API_KEY", "test")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "test")
    monkeypatch.setenv("RESEND_DOMAIN", "example.com")
    monkeypatch.setenv("BOT_FROM_LOCAL", "bot")
    monkeypatch.setenv("OWNER_FROM_LOCAL", "mail")
    monkeypatch.setenv("HERMES_SEND_BIN", str(hermes_bin))
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("BOT_REPLY_CONTEXT_DIR", raising=False)

    settings = app.bridge_settings.load_settings()

    assert settings.bridge_db == data_dir / "state.db"
    assert settings.attachment_dir == data_dir / "attachments"
    assert settings.mcp_drafts_file == data_dir / "mcp_email_drafts.json"
    assert settings.bot_reply_context_dir == data_dir / "bot_reply_contexts"
    assert settings.hermes_bridge_cache_dir == (
        app.bridge_settings.hermes_home() / "cache" / "resend-bridge"
    )


def test_hermes_task_runs_direct_subprocess(monkeypatch, tmp_path):
    commands = []
    hermes_bin = tmp_path / "hermes"
    hermes_bin.write_text(
        '#!/bin/sh\necho \'{"action":"notify","executed_task":true,"owner_report":"done"}\'',
        encoding="utf-8",
    )
    hermes_bin.chmod(0o755)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b'{"action":"notify","executed_task":true,"owner_report":"done"}', b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append(args)
        return FakeProcess()

    monkeypatch.setattr(app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, hermes_send_bin=hermes_bin, hermes_timeout_seconds=30),
    )
    monkeypatch.setattr(app, "create_outbound_message", lambda **_kwargs: 1)
    monkeypatch.setattr(app, "update_outbound_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "record_hermes_decision", lambda **_kwargs: None)
    monkeypatch.setattr(app, "record_processing_step", lambda **_kwargs: None)

    decision = asyncio.run(
        app.run_hermes_task(
            {
                "task": "test",
                "sender": "sender@example.com",
                "email": {"text_preview": "hello"},
                "attachments": [],
                "downloaded_files": [],
            },
            "email-1",
            "subject",
        )
    )

    assert commands[0][0] == str(hermes_bin)
    assert "chat" in commands[0]
    assert "--query" in commands[0]
    assert "--quiet" in commands[0]
    assert "--source" in commands[0]
    assert "tool" in commands[0]
    assert "--yolo" in commands[0]
    assert decision["action"] == "notify"
    assert decision["executed_task"] is True
    assert decision["owner_report"] == "done"


def test_create_app_can_rebind_settings(tmp_path):
    original_settings = app.SETTINGS
    original_context_dir = app.BOT_REPLY_CONTEXT_DIR
    original_generated_roots = app.GENERATED_ATTACHMENT_ROOTS
    original_cache_dir = app.HERMES_BRIDGE_CACHE_DIR
    custom = replace(
        app.SETTINGS,
        bridge_db=tmp_path / "state.db",
        attachment_dir=tmp_path / "attachments",
        bot_reply_context_dir=tmp_path / "contexts",
        generated_attachment_roots=[tmp_path / "generated"],
        hermes_bridge_cache_dir=tmp_path / "cache" / "resend-bridge",
    )

    try:
        created = app.create_app(custom)

        assert created.title == "Resend Hermes Bridge"
        assert app.SETTINGS.bridge_db == tmp_path / "state.db"
    finally:
        app.SETTINGS = original_settings
        app.BOT_REPLY_CONTEXT_DIR = original_context_dir
        app.GENERATED_ATTACHMENT_ROOTS = original_generated_roots
        app.HERMES_BRIDGE_CACHE_DIR = original_cache_dir


def test_mcp_prunes_expired_drafts():
    old = (datetime.now(UTC) - timedelta(seconds=resend_mcp_server.DRAFT_TTL_SECONDS + 60)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    drafts = {
        "old": {"created_at": old, "payload": {}},
        "fresh": {"created_at": fresh, "payload": {}},
        "bad": "not a dict",
    }

    resend_mcp_server._prune_expired_drafts(drafts)

    assert "old" not in drafts
    assert "bad" not in drafts
    assert "fresh" in drafts


def test_mcp_default_sender_is_mail():
    payload = resend_mcp_server._format_outbound_payload(
        to=["recipient@example.com"],
        subject="Hello",
        text="Hi",
    )

    assert payload["from_local"] == "mail"


def test_mcp_outbound_payload_accepts_attachment_paths():
    payload = resend_mcp_server._format_outbound_payload(
        to=["recipient@example.com"],
        subject="Hello",
        text="Hi",
        attachment_paths=["/tmp/report.txt"],
    )

    assert payload["attachments"] == [{"path": "/tmp/report.txt"}]


def test_mcp_allows_custom_sender_local_including_bot():
    custom_payload = resend_mcp_server._format_outbound_payload(
        to=["recipient@example.com"],
        subject="Hello",
        text="Hi",
        from_local="karx",
    )
    bot_payload = resend_mcp_server._format_outbound_payload(
        to=["recipient@example.com"],
        subject="Hello",
        text="Hi",
        from_local="bot",
    )

    assert custom_payload["from_local"] == "karx"
    assert bot_payload["from_local"] == "bot"


def test_mcp_rejects_direct_confirmed_send_without_draft(monkeypatch):
    calls = []

    async def fake_send(payload):
        calls.append(payload)
        return {"outbound_id": 1, "resend_id": "test"}

    monkeypatch.setattr(resend_mcp_server, "_send_via_bridge", fake_send)

    with pytest.raises(ValueError, match="requires a draft_id"):
        asyncio.run(
            resend_mcp_server.send_email(
                to=["recipient@example.com"],
                subject="Hello",
                text="Hi",
                confirmed=True,
            )
        )

    assert calls == []


def test_mcp_confirmed_draft_send_adds_hidden_approval_token(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    lock_file = tmp_path / "drafts.json.lock"
    sent_payloads = []

    async def fake_send(payload):
        sent_payloads.append(dict(payload))
        return {"outbound_id": 123, "resend_id": "resend-123"}

    monkeypatch.setattr(resend_mcp_server, "DRAFTS_FILE", drafts_file)
    monkeypatch.setattr(resend_mcp_server, "DRAFTS_LOCK_FILE", lock_file)
    monkeypatch.setattr(resend_mcp_server, "_send_via_bridge", fake_send)

    draft_result = asyncio.run(
        resend_mcp_server.send_email(
            to=["recipient@example.com"],
            subject="Hello",
            text="Hi",
        )
    )

    sent_result = asyncio.run(
        resend_mcp_server.send_email(
            to=[],
            subject="",
            draft_id=draft_result["draft_id"],
            confirmed=True,
        )
    )

    assert sent_result["status"] == "sent"
    assert sent_payloads[0]["from_local"] == "mail"
    assert sent_payloads[0]["draft_id"] == draft_result["draft_id"]
    assert sent_payloads[0]["approval_token"]


def test_mcp_bridge_send_calls_bridge(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True}

        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(resend_mcp_server.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(resend_mcp_server._send_via_bridge({"hello": "world"}))

    assert result == {"ok": True}
    assert "Authorization" not in captured["headers"]
    assert captured["headers"]["Content-Type"] == "application/json"


def _write_draft_file(path, draft_id, payload, token="token"):
    path.write_text(
        json.dumps(
            {
                draft_id: {
                    "created_at": datetime.now(UTC).isoformat(),
                    "payload": payload,
                    "approval_token": token,
                    "sent": False,
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_app_rejects_direct_manual_send_without_draft():
    with pytest.raises(app.HTTPException) as exc_info:
        app.normalize_send_payload(
            {
                "confirmed": True,
                "from_local": "mail",
                "to": ["recipient@example.com"],
                "subject": "Hello",
                "text": "Hi",
            }
        )

    assert exc_info.value.status_code == 400
    assert "draft_id and approval_token" in exc_info.value.detail


def _patch_settings_for_send_endpoint(monkeypatch, tmp_path, drafts_file):
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            bridge_db=tmp_path / "state.db",
            attachment_dir=tmp_path / "attachments",
            mcp_drafts_file=drafts_file,
            mcp_drafts_lock_file=tmp_path / "drafts.json.lock",
        ),
    )


def test_send_endpoint_accepts_manual_send(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    payload = {
        "from_local": "mail",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
    }
    _write_draft_file(drafts_file, "draft-1", payload)
    _patch_settings_for_send_endpoint(monkeypatch, tmp_path, drafts_file)
    captured = {}

    async def fake_send_resend_email(payload, *, email_id=None, step="resend_send"):
        captured["payload"] = payload
        captured["email_id"] = email_id
        captured["step"] = step
        return 123, "resend-123"

    monkeypatch.setattr(app, "send_resend_email", fake_send_resend_email)

    with TestClient(app.app) as client:
        response = client.post(
            "/send",
            json={
                **payload,
                "confirmed": True,
                "draft_id": "draft-1",
                "approval_token": "token",
            },
        )

    assert response.status_code == 200
    assert response.json()["resend_id"] == "resend-123"
    assert captured["payload"]["from"] == "mail@example.com"


def test_init_db_sets_schema_version(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, bridge_db=db_path))

    app.init_db()

    with app.open_db() as conn:
        row = conn.execute("PRAGMA user_version").fetchone()

    assert int(row[0]) == app.SCHEMA_VERSION


def test_manage_cli_status_uses_db_health(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, bridge_db=tmp_path / "state.db"))
    app.init_db()

    manage.command_status(None)

    output = capsys.readouterr().out
    assert '"ok": true' in output
    assert f'"schema_version": {app.SCHEMA_VERSION}' in output


def test_app_accepts_confirmed_manual_send_from_matching_draft(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    payload = {
        "from_local": "mail",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
    }
    _write_draft_file(drafts_file, "draft-1", payload)
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            mcp_drafts_file=drafts_file,
            mcp_drafts_lock_file=tmp_path / "drafts.json.lock",
        ),
    )

    result = app.normalize_send_payload(
        {
            **payload,
            "confirmed": True,
            "draft_id": "draft-1",
            "approval_token": "token",
        }
    )

    assert result["from"] == "mail@example.com"


def test_app_accepts_confirmed_manual_send_from_custom_sender(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    payload = {
        "from_local": "karx",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
    }
    _write_draft_file(drafts_file, "draft-1", payload)
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            mcp_drafts_file=drafts_file,
            mcp_drafts_lock_file=tmp_path / "drafts.json.lock",
        ),
    )

    result = app.normalize_send_payload(
        {
            **payload,
            "confirmed": True,
            "draft_id": "draft-1",
            "approval_token": "token",
        }
    )

    assert result["from"] == "karx@example.com"


def test_app_accepts_confirmed_manual_send_from_bot_sender(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    payload = {
        "from_local": "bot",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
    }
    _write_draft_file(drafts_file, "draft-1", payload)
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            mcp_drafts_file=drafts_file,
            mcp_drafts_lock_file=tmp_path / "drafts.json.lock",
        ),
    )

    result = app.normalize_send_payload(
        {
            **payload,
            "confirmed": True,
            "draft_id": "draft-1",
            "approval_token": "token",
        }
    )

    assert result["from"] == "bot@example.com"


def test_app_normalizes_path_attachments(monkeypatch, tmp_path):
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello attachment", encoding="utf-8")
    drafts_file = tmp_path / "drafts.json"
    payload = {
        "from_local": "mail",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
        "attachments": [{"path": str(attachment), "content_type": "text/plain"}],
    }
    _write_draft_file(drafts_file, "draft-1", payload)
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            mcp_drafts_file=drafts_file,
            mcp_drafts_lock_file=tmp_path / "drafts.json.lock",
        ),
    )

    result = app.normalize_send_payload(
        {
            **payload,
            "confirmed": True,
            "draft_id": "draft-1",
            "approval_token": "token",
        }
    )

    assert result["attachments"][0]["filename"] == "report.txt"
    assert result["attachments"][0]["content"] == "aGVsbG8gYXR0YWNobWVudA=="
    assert result["attachments"][0]["content_type"] == "text/plain"


def test_app_rejects_auto_reply_attachment_outside_inbound_dir(monkeypatch, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, attachment_dir=tmp_path / "attachments"),
    )

    with pytest.raises(app.HTTPException) as exc_info:
        app.normalize_send_payload(
            {
                "confirmed": True,
                "auto_reply_email_id": "email-1",
                "from_local": "bot",
                "to": ["sender@example.com"],
                "subject": "Re: Hello",
                "text": "Hi",
                "attachments": [{"path": str(outside)}],
            },
            allow_bot_sender=True,
        )

    assert exc_info.value.status_code == 400
    assert "must come from this inbound email" in exc_info.value.detail


def test_auto_reply_can_attach_downloaded_file_by_reply_attachments(tmp_path):
    attachment = tmp_path / "attachments" / "email-1" / "report.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("report", encoding="utf-8")

    payload = app.build_resend_reply_payload(
        {
            "from": "sender@example.com",
            "subject": "请转发附件",
            "message_id": "<message@example.com>",
            "headers": {},
        },
        {
            "action": "reply",
            "reply_text": "见附件",
            "reply_attachments": [str(attachment)],
            "_downloaded_files": [
                {
                    "id": "att-1",
                    "filename": "report.txt",
                    "content_type": "text/plain",
                    "local_path": str(attachment),
                }
            ],
        },
    )

    assert payload["attachments"] == [
        {
            "path": str(attachment),
            "filename": "report.txt",
            "content_type": "text/plain",
        }
    ]


def test_auto_reply_ignores_removed_forward_received_attachments_field(tmp_path):
    attachment = tmp_path / "attachments" / "email-1" / "report.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("report", encoding="utf-8")

    payload = app.build_resend_reply_payload(
        {
            "from": "sender@example.com",
            "subject": "请转发附件",
            "message_id": "<message@example.com>",
            "headers": {},
        },
        {
            "action": "reply",
            "reply_text": "见附件",
            "forward_received_attachments": True,
            "_downloaded_files": [
                {
                    "id": "att-1",
                    "filename": "report.txt",
                    "content_type": "text/plain",
                    "local_path": str(attachment),
                }
            ],
        },
    )

    assert "attachments" not in payload


def test_auto_reply_materializes_missing_text_attachment(monkeypatch, tmp_path):
    generated_root = tmp_path / "generated"
    attachment = generated_root / "image_description.txt"
    original_generated_roots = app.GENERATED_ATTACHMENT_ROOTS
    monkeypatch.setattr(app, "GENERATED_ATTACHMENT_ROOTS", [generated_root])

    try:
        payload = app.build_resend_reply_payload(
            {
                "from": "sender@example.com",
                "subject": "告诉我图片是什么",
                "message_id": "<message@example.com>",
                "headers": {},
            },
            {
                "action": "reply",
                "owner_report": "图片内容描述：一张测试图片。",
                "reply_text": "图片描述已生成，见附件。",
                "reply_attachments": [str(attachment)],
            },
        )
    finally:
        app.GENERATED_ATTACHMENT_ROOTS = original_generated_roots

    assert attachment.read_text(encoding="utf-8") == "图片内容描述：一张测试图片。\n"
    assert payload["attachments"] == [{"path": str(attachment)}]


def test_auto_reply_skips_invalid_generated_attachment(monkeypatch, tmp_path):
    generated_root = tmp_path / "generated"
    missing_image = generated_root / "image.png"
    original_generated_roots = app.GENERATED_ATTACHMENT_ROOTS
    decision = {
        "action": "reply",
        "owner_report": "已处理。",
        "reply_text": "已处理。",
        "reply_attachments": [str(missing_image)],
    }
    monkeypatch.setattr(app, "GENERATED_ATTACHMENT_ROOTS", [generated_root])

    try:
        payload = app.build_resend_reply_payload(
            {
                "from": "sender@example.com",
                "subject": "生成图片",
                "message_id": "<message@example.com>",
                "headers": {},
            },
            decision,
        )
    finally:
        app.GENERATED_ATTACHMENT_ROOTS = original_generated_roots

    assert "attachments" not in payload
    assert "自动回复时跳过了无效附件" in decision["owner_report"]
    assert str(missing_image) in decision["owner_report"]


def test_record_fetched_attachment_metadata_updates_download_status(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, bridge_db=db_path))
    app.init_db()
    app.record_inbound_email(
        svix_id="svix-1",
        event={"type": "email.received", "data": {"email_id": "email-1"}},
        email={
            "id": "email-1",
            "from": "sender@example.com",
            "to": ["bot@example.com"],
            "subject": "Hello",
        },
        attachments=[],
        addressed_to_inbound=True,
    )

    attachment = {
        "id": "att-1",
        "filename": "report.txt",
        "content_type": "text/plain",
        "size": 12,
        "download_url": "https://example.com/report.txt",
    }
    app.record_fetched_attachment_metadata("email-1", [attachment])
    app.record_attachment_history(
        email_id="email-1",
        raw_attachment=attachment,
        item={
            "id": "att-1",
            "filename": "report.txt",
            "content_type": "text/plain",
            "size": 12,
            "relevant": True,
            "local_path": "/tmp/report.txt",
            "text_snippet": "hello",
        },
    )

    with app.open_db() as conn:
        rows = list(conn.execute("SELECT * FROM attachments WHERE email_id = ?", ("email-1",)))

    assert len(rows) == 1
    assert rows[0]["attachment_id"] == "att-1"
    assert rows[0]["local_path"] == "/tmp/report.txt"
    assert rows[0]["text_snippet"] == "hello"
