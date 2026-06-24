import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app
from scripts import manage


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

from svix.webhooks import Webhook  # noqa: E402

import resend_mcp_server  # noqa: E402
import services.hermes_context as hermes_context  # noqa: E402
import services.inbound_email as inbound_email_service  # noqa: E402
import services.mailbox_store as mailbox_store  # noqa: E402
import services.notification as notification_service  # noqa: E402
import services.telegram_rich as telegram_rich  # noqa: E402
import utils.email_display as notices  # noqa: E402


def generate_svix_headers(secret: str, payload: bytes) -> dict[str, str]:
    webhook_id = secrets.token_hex(16)
    timestamp = datetime.now(UTC)
    signature = Webhook(secret).sign(
        msg_id=webhook_id,
        timestamp=timestamp,
        data=payload.decode(),
    )
    return {
        "svix-id": webhook_id,
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": signature,
    }


def test_mcp_dependency_is_available_to_runtime_python():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import resend_mcp_server; print('ok')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "ok" in completed.stdout


def test_generate_svix_headers_generates_valid_signature():
    secret = "whsec_" + base64.b64encode(b"test-secret").decode("ascii")
    payload = {"type": "email.received", "data": {"email_id": "email-1"}}
    body = json.dumps(payload).encode()

    verified = Webhook(secret).verify(body.decode(), generate_svix_headers(secret, body))

    assert verified == payload


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


def test_bot_auto_reply_uses_configured_bot_sender(monkeypatch):
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, bot_from_local="assistant"))

    payload = app.build_resend_reply_payload(
        {
            "from": "user@example.com",
            "subject": "测试",
            "message_id": "<message@example.com>",
            "headers": {},
        },
        {
            "action": "reply",
            "reply_text": "已处理",
        },
    )
    normalized = app.normalize_send_payload(
        {**payload, "confirmed": True, "auto_reply_email_id": "email-1"},
        allow_bot_sender=True,
    )

    assert payload["from_local"] == "assistant"
    assert normalized["from"] == f"assistant@{app.SETTINGS.resend_domain}"


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
    sent_attachment_paths = []
    statuses = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        messages.append(message)
        sent_attachment_paths.append(attachment_paths)

    async def fake_download_attachments_for_notification(_email_id, _attachments):
        return []

    def fake_update_status(email_id, status, error=None):
        statuses.append((email_id, status, error))

    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(
        inbound_email_service,
        "download_attachments_for_notification",
        fake_download_attachments_for_notification,
    )
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
    assert sent_attachment_paths == [[]]
    assert statuses == [("email-1", "notified", None)]


def test_non_bot_email_notice_sends_downloaded_attachment_paths(monkeypatch):
    notifications = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        notifications.append(
            {
                "message": message,
                "email_id": email_id,
                "attachment_paths": attachment_paths,
            }
        )

    async def fake_download_attachments_for_notification(email_id, attachments):
        assert email_id == "email-1"
        assert attachments == [{"filename": "image.jpg"}]
        return [
            {"local_path": "/tmp/image.jpg"},
            {"filename": "skipped.txt"},
        ]

    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(
        inbound_email_service,
        "download_attachments_for_notification",
        fake_download_attachments_for_notification,
    )
    monkeypatch.setattr(app, "update_inbound_status", lambda *_args, **_kwargs: None)

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
            [{"filename": "image.jpg"}],
        )
    )

    assert len(notifications) == 1
    assert notifications[0]["message"].startswith("主人你有一封新邮件~")
    assert notifications[0]["email_id"] == "email-1"
    assert notifications[0]["attachment_paths"] == ["/tmp/image.jpg"]


def test_bot_email_notice_uses_kabao_title(monkeypatch):
    messages = []
    sent_attachment_paths = []
    statuses = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        messages.append(message)
        sent_attachment_paths.append(attachment_paths)

    async def fake_download_attachments_for_notification(_email_id, _attachments):
        return []

    def fake_update_status(email_id, status, error=None):
        statuses.append((email_id, status, error))

    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(
        inbound_email_service,
        "download_attachments_for_notification",
        fake_download_attachments_for_notification,
    )
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
    assert sent_attachment_paths == [[]]
    assert statuses == [("email-1", "processing", None)]


def test_bot_email_notice_ignores_custom_title_env(monkeypatch):
    messages = []

    async def fake_notify(message, *, email_id=None, attachment_paths=None):
        messages.append(message)

    async def fake_download_attachments_for_notification(_email_id, _attachments):
        return []

    monkeypatch.setenv("NOTIFICATION_BOT_TITLE", "不应该生效")
    monkeypatch.setattr(app, "notify_telegram", fake_notify)
    monkeypatch.setattr(
        inbound_email_service,
        "download_attachments_for_notification",
        fake_download_attachments_for_notification,
    )
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


def test_expired_bot_reply_context_is_rejected_and_removed(monkeypatch, tmp_path):
    context_dir = tmp_path / "bot_reply_contexts"
    context_dir.mkdir()
    monkeypatch.setattr(app, "BOT_REPLY_CONTEXT_DIR", context_dir)
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, bot_reply_context_ttl_seconds=60),
    )
    path = context_dir / "email-1.json"
    path.write_text(
        json.dumps(
            {
                "email_id": "email-1",
                "sender": app.SETTINGS.inbound_address,
                "reply_to": "sender@example.com",
                "created_at": (datetime.now(UTC) - timedelta(seconds=120)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    assert (
        app._is_authorized_bot_reply(
            {
                "auto_reply_email_id": "email-1",
                "from_local": app.SETTINGS.bot_from_local,
                "to": ["sender@example.com"],
            }
        )
        is False
    )
    assert not path.exists()


def test_notify_telegram_uses_hermes_send_by_default(monkeypatch, tmp_path):
    commands = []
    updates = []
    steps = []
    context_calls = []
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

    async def fake_send_telegram_rich_text(_target, _body):
        return telegram_rich.TelegramRichSendResult(
            sent=False,
            reason="not rich eligible",
        )

    monkeypatch.setattr(
        app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        notification_service,
        "send_telegram_rich_text",
        fake_send_telegram_rich_text,
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

    def fake_append_notification_to_user_context(target, body):
        context_calls.append((target, body))
        return hermes_context.HermesContextAppendResult(
            recorded=True,
            session_id="session-1",
            message_id=123,
        )

    monkeypatch.setattr(
        notification_service,
        "append_notification_to_user_context",
        fake_append_notification_to_user_context,
    )

    asyncio.run(app.notify_telegram(message, email_id="email-1"))

    context_message = notification_service.context_message_for_notification(
        "telegram",
        "email-1",
        message,
        kind="text",
    )
    assert commands == [(str(hermes_bin), "send", "--to", "telegram", message)]
    assert context_calls == [("telegram", context_message)]
    assert updates[-1][0] == 99
    assert updates[-1][1]["status"] == app.OutboundStatus.SENT
    assert updates[-1][1]["response"]["context"] == [
        {
            "recorded": True,
            "session_id": "session-1",
            "message_id": 123,
            "kind": "text",
        }
    ]
    assert steps[0] == {
        "step": "telegram_context",
        "status": app.StepStatus.DONE,
        "email_id": "email-1",
        "detail": {
            "recorded": True,
            "session_id": "session-1",
            "message_id": 123,
            "kind": "text",
        },
    }
    assert steps[-1]["step"] == "telegram_notify"


def test_notify_telegram_uses_rich_send_when_available(monkeypatch, tmp_path):
    commands = []
    updates = []
    steps = []
    context_calls = []
    message = "| 字段 | 内容 |\n| --- | --- |\n| From | sender@example.com |"
    hermes_bin = tmp_path / "hermes"
    hermes_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes_bin.chmod(0o755)

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append(args)
        raise AssertionError("hermes send should not be called after rich send")

    async def fake_send_telegram_rich_text(target, body):
        assert target == "telegram"
        assert body == message
        return telegram_rich.TelegramRichSendResult(
            sent=True,
            stdout="telegram rich sent message_id=321\n",
            message_id="321",
        )

    def fake_append_notification_to_user_context(target, body):
        context_calls.append((target, body))
        return hermes_context.HermesContextAppendResult(
            recorded=True,
            session_id="session-1",
            message_id=321,
        )

    monkeypatch.setattr(
        app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        notification_service,
        "send_telegram_rich_text",
        fake_send_telegram_rich_text,
    )
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, notification_target="telegram", hermes_send_bin=hermes_bin),
    )
    monkeypatch.setattr(app, "create_outbound_message", lambda **_kwargs: 103)
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
    monkeypatch.setattr(
        notification_service,
        "append_notification_to_user_context",
        fake_append_notification_to_user_context,
    )

    asyncio.run(app.notify_telegram(message, email_id="email-rich"))

    context_message = notification_service.context_message_for_notification(
        "telegram",
        "email-rich",
        message,
        kind="text",
    )
    assert commands == []
    assert context_calls == [("telegram", context_message)]
    assert updates[-1][0] == 103
    assert updates[-1][1]["status"] == app.OutboundStatus.SENT
    assert updates[-1][1]["stdout"] == "telegram rich sent message_id=321\n"
    assert steps[0]["step"] == "telegram_context"
    assert steps[-1]["step"] == "telegram_notify"


def test_telegram_rich_payload_is_available_without_hermes_adapter():
    message = "主人你有一封新邮件~\n\n| 字段 | 内容 |\n| --- | --- |\n| From | sender@example.com |"

    assert telegram_rich._rich_skip_reason(message, {"rich_messages": True}) is None
    assert telegram_rich._rich_message_payload(message) == {"markdown": message}


def test_notification_context_uses_raw_message_text():
    message = "邮件处理结果"

    context_message = notification_service.context_message_for_notification(
        "telegram",
        "email-1",
        message,
        kind="text",
    )

    assert context_message == message


def test_notify_telegram_does_not_fail_when_context_recording_fails(monkeypatch, tmp_path):
    commands = []
    updates = []
    steps = []
    message = "邮件处理结果"
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

    async def fake_send_telegram_rich_text(_target, _body):
        return telegram_rich.TelegramRichSendResult(
            sent=False,
            reason="not rich eligible",
        )

    def fail_context_recording(_target, _body):
        raise RuntimeError("context db locked")

    monkeypatch.setattr(
        app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        notification_service,
        "send_telegram_rich_text",
        fake_send_telegram_rich_text,
    )
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, notification_target="telegram", hermes_send_bin=hermes_bin),
    )
    monkeypatch.setattr(app, "create_outbound_message", lambda **_kwargs: 100)
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
    monkeypatch.setattr(
        notification_service,
        "append_notification_to_user_context",
        fail_context_recording,
    )

    asyncio.run(app.notify_telegram(message, email_id="email-2"))

    assert commands == [(str(hermes_bin), "send", "--to", "telegram", message)]
    assert updates[-1][0] == 100
    assert updates[-1][1]["status"] == app.OutboundStatus.SENT
    assert steps[0]["step"] == "telegram_context"
    assert steps[0]["status"] == app.StepStatus.FAILED
    assert steps[0]["error"] == "context db locked"
    assert steps[-1]["step"] == "telegram_notify"


def test_notify_telegram_records_context_before_media_failure(monkeypatch, tmp_path):
    commands = []
    updates = []
    steps = []
    context_calls = []
    message = "邮件处理结果"
    attachment_path = "/tmp/report.pdf"
    hermes_bin = tmp_path / "hermes"
    hermes_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes_bin.chmod(0o755)

    class FakeProcess:
        def __init__(self, returncode, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout, self._stderr

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append(args)
        if args[-1].startswith("MEDIA:"):
            return FakeProcess(1, stderr=b"media failed")
        return FakeProcess(0, stdout=b"sent\n")

    async def fake_send_telegram_rich_text(_target, _body):
        return telegram_rich.TelegramRichSendResult(
            sent=False,
            reason="not rich eligible",
        )

    def fake_append_notification_to_user_context(target, body):
        context_calls.append((target, body))
        return hermes_context.HermesContextAppendResult(
            recorded=True,
            session_id="session-1",
            message_id=124,
        )

    monkeypatch.setattr(
        app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        notification_service,
        "send_telegram_rich_text",
        fake_send_telegram_rich_text,
    )
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, notification_target="telegram", hermes_send_bin=hermes_bin),
    )
    monkeypatch.setattr(app, "create_outbound_message", lambda **_kwargs: 101)
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
    monkeypatch.setattr(
        notification_service,
        "append_notification_to_user_context",
        fake_append_notification_to_user_context,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            app.notify_telegram(
                message,
                email_id="email-3",
                attachment_paths=[attachment_path],
            )
        )

    assert commands == [
        (str(hermes_bin), "send", "--to", "telegram", message),
        (str(hermes_bin), "send", "--to", "telegram", f"MEDIA:{attachment_path}"),
    ]
    assert context_calls == [
        (
            "telegram",
            notification_service.context_message_for_notification(
                "telegram",
                "email-3",
                message,
                kind="text",
            ),
        )
    ]
    assert steps[0]["step"] == "telegram_context"
    assert steps[0]["status"] == app.StepStatus.DONE
    assert updates[-1][0] == 101
    assert updates[-1][1]["status"] == app.OutboundStatus.FAILED
    assert steps[-1]["step"] == "telegram_notify"
    assert steps[-1]["status"] == app.StepStatus.FAILED


def test_notify_telegram_records_successful_media_in_context(monkeypatch, tmp_path):
    commands = []
    updates = []
    steps = []
    context_calls = []
    message = "邮件处理结果"
    attachment_path = "/tmp/report.pdf"
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

    async def fake_send_telegram_rich_text(_target, _body):
        return telegram_rich.TelegramRichSendResult(
            sent=False,
            reason="not rich eligible",
        )

    def fake_append_notification_to_user_context(target, body):
        context_calls.append((target, body))
        return hermes_context.HermesContextAppendResult(
            recorded=True,
            session_id="session-1",
            message_id=200 + len(context_calls),
        )

    monkeypatch.setattr(
        app.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(
        notification_service,
        "send_telegram_rich_text",
        fake_send_telegram_rich_text,
    )
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, notification_target="telegram", hermes_send_bin=hermes_bin),
    )
    monkeypatch.setattr(app, "create_outbound_message", lambda **_kwargs: 102)
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
    monkeypatch.setattr(
        notification_service,
        "append_notification_to_user_context",
        fake_append_notification_to_user_context,
    )

    asyncio.run(
        app.notify_telegram(
            message,
            email_id="email-4",
            attachment_paths=[attachment_path],
        )
    )

    media_message = f"MEDIA:{attachment_path}"
    assert commands == [
        (str(hermes_bin), "send", "--to", "telegram", message),
        (str(hermes_bin), "send", "--to", "telegram", media_message),
    ]
    assert context_calls == [
        (
            "telegram",
            notification_service.context_message_for_notification(
                "telegram",
                "email-4",
                message,
                kind="text",
            ),
        ),
        (
            "telegram",
            notification_service.context_message_for_notification(
                "telegram",
                "email-4",
                media_message,
                kind="media",
                path=attachment_path,
            ),
        ),
    ]
    assert updates[-1][0] == 102
    assert updates[-1][1]["status"] == app.OutboundStatus.SENT
    assert updates[-1][1]["response"]["context"] == [
        {
            "recorded": True,
            "session_id": "session-1",
            "message_id": 201,
            "kind": "text",
        },
        {
            "recorded": True,
            "session_id": "session-1",
            "message_id": 202,
            "kind": "media",
            "path": attachment_path,
        },
    ]
    assert steps[0]["detail"]["kind"] == "text"
    assert steps[1]["detail"]["kind"] == "media"
    assert steps[1]["detail"]["path"] == attachment_path
    assert steps[-1]["step"] == "telegram_notify"


def test_resolve_session_id_for_notification_target(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    sessions_dir = hermes_home / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sessions.json").write_text(
        json.dumps(
            {
                "agent:main:telegram:dm:111": {
                    "session_id": "old-session",
                    "platform": "telegram",
                    "chat_type": "dm",
                    "updated_at": "2026-01-01T00:00:00",
                    "origin": {"platform": "telegram", "chat_id": "111", "chat_type": "dm"},
                },
                "agent:main:telegram:dm:222": {
                    "session_id": "new-session",
                    "platform": "telegram",
                    "chat_type": "dm",
                    "updated_at": "2026-01-02T00:00:00",
                    "origin": {"platform": "telegram", "chat_id": "222", "chat_type": "dm"},
                },
                "agent:main:weixin:dm:user": {
                    "session_id": "weixin-session",
                    "platform": "weixin",
                    "chat_type": "dm",
                    "updated_at": "2026-01-03T00:00:00",
                    "origin": {"platform": "weixin", "chat_id": "user", "chat_type": "dm"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_context.bridge_settings, "hermes_home", lambda: hermes_home)

    assert hermes_context.resolve_session_id_for_target("telegram") == "new-session"
    assert hermes_context.resolve_session_id_for_target("telegram:111") == "old-session"
    assert hermes_context.resolve_session_id_for_target("telegram:dm:222") == "new-session"
    assert hermes_context.resolve_session_id_for_target("signal") is None


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
    assert "Resend ID: `reply-123`" in notice
    assert "Email ID" not in notice
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

def test_load_settings_uses_project_data_dir(monkeypatch, tmp_path):
    hermes_bin = tmp_path / "hermes"
    hermes_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    hermes_bin.chmod(0o755)
    data_dir = app.bridge_settings.APP_DIR / "data"

    monkeypatch.setenv("RESEND_API_KEY", "test")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "test")
    monkeypatch.setenv("RESEND_DOMAIN", "example.com")
    monkeypatch.setenv("BOT_FROM_LOCAL", "bot")
    monkeypatch.setenv("OWNER_FROM_LOCAL", "mail")
    monkeypatch.setenv(
        "PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    settings = app.bridge_settings.load_settings()

    assert settings.hermes_send_bin == hermes_bin
    assert settings.bridge_db == data_dir / "state.db"
    assert settings.attachment_dir == data_dir / "attachments"
    assert settings.mcp_drafts_file == data_dir / "mcp_email_drafts.json"
    assert settings.bot_reply_context_dir == data_dir / "bot_reply_contexts"
    assert settings.bot_reply_context_ttl_seconds == 600
    assert settings.mcp_draft_ttl_seconds == 604800
    assert settings.generated_attachment_roots == [data_dir / "generated"]
    assert "web" in settings.hermes_email_task_toolsets


def test_hermes_task_runs_direct_subprocess(monkeypatch, tmp_path):
    commands = []
    subprocess_kwargs = []
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
        subprocess_kwargs.append(kwargs)
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
    assert "--toolsets" in commands[0]
    toolsets_value = commands[0][commands[0].index("--toolsets") + 1]
    assert "resend_email" not in toolsets_value
    assert "--source" in commands[0]
    assert "tool" in commands[0]
    assert "--yolo" in commands[0]
    assert subprocess_kwargs[0]["env"]["HERMES_SESSION_SOURCE"] == "tool"
    assert decision["action"] == "notify"
    assert decision["executed_task"] is True
    assert decision["owner_report"] == "done"


def test_create_app_can_rebind_settings(tmp_path):
    original_settings = app.SETTINGS
    original_context_dir = app.BOT_REPLY_CONTEXT_DIR
    original_generated_roots = app.GENERATED_ATTACHMENT_ROOTS
    custom = replace(
        app.SETTINGS,
        bridge_db=tmp_path / "state.db",
        attachment_dir=tmp_path / "attachments",
        bot_reply_context_dir=tmp_path / "contexts",
        generated_attachment_roots=[tmp_path / "generated"],
    )

    try:
        created = app.create_app(custom)

        assert created.title == "Resend Hermes Bridge"
        assert app.SETTINGS.bridge_db == tmp_path / "state.db"
    finally:
        app.SETTINGS = original_settings
        app.BOT_REPLY_CONTEXT_DIR = original_context_dir
        app.GENERATED_ATTACHMENT_ROOTS = original_generated_roots


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


def test_mcp_rejects_chat_preview_template_as_body():
    with pytest.raises(ValueError, match="chat preview or confirmation template"):
        resend_mcp_server._format_outbound_payload(
            to=["recipient@example.com"],
            subject="Hello",
            text="邮件草稿已创建，请确认：\n\n| 项目 | 内容 |\n| --- | --- |",
        )


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
                confirmed=True,
            )
        )

    assert calls == []


def test_mcp_draft_success_returns_minimal_preview_marker(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    lock_file = tmp_path / "drafts.json.lock"
    shown = []

    async def fake_show(payload, *, draft_id, title="", footer=""):
        shown.append(
            {
                "payload": dict(payload),
                "draft_id": draft_id,
                "title": title,
                "footer": footer,
            }
        )

    monkeypatch.setattr(resend_mcp_server, "DRAFTS_FILE", drafts_file)
    monkeypatch.setattr(resend_mcp_server, "DRAFTS_LOCK_FILE", lock_file)
    monkeypatch.setattr(resend_mcp_server, "_show_draft_via_bridge", fake_show)

    result = asyncio.run(
        resend_mcp_server.send_email(
            to=["recipient@example.com"],
            subject="Hello",
            text="Hi",
        )
    )

    assert result == {
        "status": "drafted",
        "draft_id": result["draft_id"],
        "preview_delivered": True,
    }
    assert shown[0]["payload"]["to"] == ["recipient@example.com"]
    assert shown[0]["payload"]["subject"] == "Hello"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "assistant_response" not in serialized
    assert "display" not in serialized
    assert "metadata" not in serialized
    assert "next_step" not in serialized
    assert "recipient@example.com" not in serialized
    assert "Hello" not in serialized
    assert "Hi" not in serialized


def test_mcp_revision_creates_new_draft_and_links_previous(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    lock_file = tmp_path / "drafts.json.lock"
    shown = []

    async def fake_show(payload, *, draft_id, title="", footer=""):
        shown.append({"payload": dict(payload), "draft_id": draft_id})

    monkeypatch.setattr(resend_mcp_server, "DRAFTS_FILE", drafts_file)
    monkeypatch.setattr(resend_mcp_server, "DRAFTS_LOCK_FILE", lock_file)
    monkeypatch.setattr(resend_mcp_server, "_show_draft_via_bridge", fake_show)

    first = asyncio.run(
        resend_mcp_server.send_email(
            to=["recipient@example.com"],
            subject="Hello",
            text="First body",
        )
    )
    second = asyncio.run(
        resend_mcp_server.send_email(
            to=["recipient@example.com"],
            subject="Revised",
            text="Second body",
            revision_of=first["draft_id"],
        )
    )

    assert second["draft_id"] != first["draft_id"]
    assert shown[-1]["payload"]["subject"] == "Revised"
    data = json.loads(drafts_file.read_text(encoding="utf-8"))
    assert data[second["draft_id"]]["revision_of"] == first["draft_id"]
    assert second["draft_id"] in data[first["draft_id"]]["revisions"]


def test_mcp_draft_id_with_new_payload_requires_revision_of(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    lock_file = tmp_path / "drafts.json.lock"

    async def fake_show(_payload, *, draft_id, title="", footer=""):
        return None

    monkeypatch.setattr(resend_mcp_server, "DRAFTS_FILE", drafts_file)
    monkeypatch.setattr(resend_mcp_server, "DRAFTS_LOCK_FILE", lock_file)
    monkeypatch.setattr(resend_mcp_server, "_show_draft_via_bridge", fake_show)

    first = asyncio.run(
        resend_mcp_server.send_email(
            to=["recipient@example.com"],
            subject="Hello",
            text="First body",
        )
    )

    with pytest.raises(ValueError, match="revision_of"):
        asyncio.run(
            resend_mcp_server.send_email(
                to=["recipient@example.com"],
                subject="Changed",
                text="Changed body",
                draft_id=first["draft_id"],
            )
        )


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


def test_app_rejects_confirmed_manual_send_with_unreviewed_headers(monkeypatch, tmp_path):
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

    with pytest.raises(app.HTTPException) as exc_info:
        app.normalize_send_payload(
            {
                **payload,
                "headers": {"X-Extra": "not shown in draft"},
                "confirmed": True,
                "draft_id": "draft-1",
                "approval_token": "token",
            }
        )

    assert exc_info.value.status_code == 400
    assert "send payload does not match draft_id" in exc_info.value.detail


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


def test_send_endpoint_marks_draft_sent_and_rejects_reuse(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    payload = {
        "from_local": "mail",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
    }
    _write_draft_file(drafts_file, "draft-1", payload)
    _patch_settings_for_send_endpoint(monkeypatch, tmp_path, drafts_file)
    sent_payloads = []

    async def fake_send_resend_email(payload, *, email_id=None, step="resend_send"):
        sent_payloads.append(payload)
        return 123, "resend-123"

    monkeypatch.setattr(app, "send_resend_email", fake_send_resend_email)

    request_body = {
        **payload,
        "confirmed": True,
        "draft_id": "draft-1",
        "approval_token": "token",
    }
    with TestClient(app.app) as client:
        first = client.post("/send", json=request_body)
        second = client.post("/send", json=request_body)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "already sent" in second.json()["detail"]
    assert len(sent_payloads) == 1
    stored = json.loads(drafts_file.read_text(encoding="utf-8"))["draft-1"]
    assert stored["sent"] is True
    assert stored["sending"] is False
    assert stored["bridge_response"]["resend_id"] == "resend-123"


def test_send_endpoint_rejects_non_object_json(monkeypatch, tmp_path):
    _patch_settings_for_send_endpoint(monkeypatch, tmp_path, tmp_path / "drafts.json")

    with TestClient(app.app) as client:
        response = client.post("/send", json=[])

    assert response.status_code == 400
    assert response.json()["detail"] == "JSON object body is required"


def test_show_draft_endpoint_renders_and_notifies(monkeypatch, tmp_path):
    drafts_file = tmp_path / "drafts.json"
    _patch_settings_for_send_endpoint(monkeypatch, tmp_path, drafts_file)
    captured = {}

    async def fake_notify_telegram(message, *, email_id=None, attachment_paths=None):
        captured["message"] = message
        captured["email_id"] = email_id
        captured["attachment_paths"] = attachment_paths

    monkeypatch.setattr(app, "notify_telegram", fake_notify_telegram)

    payload = {
        "from_local": "mail",
        "to": ["recipient@example.com"],
        "subject": "Hello",
        "text": "Hi",
    }
    with TestClient(app.app) as client:
        response = client.post(
            "/show-draft",
            json={
                "payload": payload,
                "draft_id": "draft-1",
                "title": "请确认是否发送以下邮件：",
                "footer": "确认后发送。",
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "请确认是否发送以下邮件：" in captured["message"]
    assert "| 字段 | 内容 |" in captured["message"]
    assert "Draft ID" in captured["message"]
    assert "确认后发送。" in captured["message"]
    assert captured["email_id"] is None


def test_show_draft_endpoint_rejects_non_object_json(monkeypatch, tmp_path):
    _patch_settings_for_send_endpoint(monkeypatch, tmp_path, tmp_path / "drafts.json")

    with TestClient(app.app) as client:
        response = client.post("/show-draft", json=[])

    assert response.status_code == 400
    assert response.json()["detail"] == "JSON object body is required"


def test_init_db_creates_current_schema(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, bridge_db=db_path))

    app.init_db()

    with app.open_db() as conn:
        inbound_columns = {
            str(item["name"]) for item in conn.execute("PRAGMA table_info(inbound_emails)")
        }
        outbound_columns = {
            str(item["name"]) for item in conn.execute("PRAGMA table_info(outbound_messages)")
        }
        label_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_labels'"
        ).fetchone()

    assert {"deleted_at", "deleted_reason"}.issubset(inbound_columns)
    assert {"deleted_at", "deleted_reason"}.issubset(outbound_columns)
    assert label_table is not None


def test_mailbox_store_search_labels_and_soft_delete(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            bridge_db=db_path,
            attachment_dir=tmp_path / "attachments",
        ),
    )
    app.init_db()
    app.record_inbound_email(
        svix_id="svix-1",
        event={"type": "email.received", "data": {"email_id": "email-1"}},
        email={
            "id": "email-1",
            "from": "sender@example.com",
            "to": ["bot@example.com"],
            "subject": "Quarterly report",
            "text": "The report is ready.",
        },
        attachments=[],
        addressed_to_inbound=True,
    )

    result = mailbox_store.search_mailbox(db_path=db_path, query="quarterly")
    assert result["total"] == 1
    assert result["items"][0]["message_id"] == "email-1"

    labels = mailbox_store.update_mailbox_labels(
        db_path=db_path,
        kind="inbound",
        message_id="email-1",
        add_labels=["finance", "todo"],
    )
    assert labels["labels"] == ["finance", "todo"]
    labeled = mailbox_store.search_mailbox(db_path=db_path, label="finance")
    assert labeled["total"] == 1

    detail = mailbox_store.get_mailbox_email(
        db_path=db_path,
        kind="inbound",
        message_id="email-1",
    )
    assert detail["subject"] == "Quarterly report"
    assert detail["labels"] == ["finance", "todo"]

    deleted = mailbox_store.delete_mailbox_email(
        db_path=db_path,
        kind="inbound",
        message_id="email-1",
        reason="archived",
    )
    assert deleted["deleted"] is True
    assert mailbox_store.search_mailbox(db_path=db_path, query="quarterly")["total"] == 0
    assert (
        mailbox_store.search_mailbox(
            db_path=db_path,
            query="quarterly",
            include_deleted=True,
        )["total"]
        == 1
    )


def test_mailbox_store_list_mailbox_sorts_pages_and_aliases(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            bridge_db=db_path,
            attachment_dir=tmp_path / "attachments",
        ),
    )
    app.init_db()
    app.record_inbound_email(
        svix_id="svix-old",
        event={"type": "email.received", "data": {"email_id": "email-old"}},
        email={
            "id": "email-old",
            "from": "old@example.com",
            "to": ["mail@example.com"],
            "subject": "Old inbox",
            "text": "Old body",
        },
        attachments=[],
        addressed_to_inbound=False,
    )
    app.record_inbound_email(
        svix_id="svix-mid",
        event={"type": "email.received", "data": {"email_id": "email-mid"}},
        email={
            "id": "email-mid",
            "from": "mid@example.com",
            "to": ["mail@example.com"],
            "subject": "Mid inbox",
            "text": "Mid body",
        },
        attachments=[],
        addressed_to_inbound=False,
    )
    outbound_id = app.create_outbound_message(
        kind="manual",
        email_id=None,
        recipient="recipient@example.com",
        subject="Latest sent",
        body_text="Latest body",
        payload={"text": "Latest body"},
    )
    with app.open_db() as conn:
        conn.execute(
            "UPDATE inbound_emails SET received_at = ?, updated_at = ? WHERE email_id = ?",
            ("2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00", "email-old"),
        )
        conn.execute(
            "UPDATE inbound_emails SET received_at = ?, updated_at = ? WHERE email_id = ?",
            ("2026-01-03T10:00:00+00:00", "2026-01-03T10:00:00+00:00", "email-mid"),
        )
        conn.execute(
            """
            UPDATE outbound_messages
            SET status = ?, created_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                app.OutboundStatus.SENT,
                "2026-01-05T10:00:00+00:00",
                "2026-01-05T10:00:00+00:00",
                outbound_id,
            ),
        )

    first_page = mailbox_store.list_mailbox(db_path=db_path, mailbox="邮件箱", limit=2)
    assert first_page["mailbox"] == "all"
    assert first_page["total"] == 3
    assert first_page["count"] == 2
    assert first_page["has_more"] is True
    assert first_page["next_offset"] == 2
    assert [item["subject"] for item in first_page["items"]] == [
        "Latest sent",
        "Mid inbox",
    ]
    assert "_search_text" not in first_page["items"][0]
    assert "按最新时间排序" in first_page["display"]

    second_page = mailbox_store.list_mailbox(
        db_path=db_path,
        mailbox="all",
        limit=2,
        offset=2,
    )
    assert [item["subject"] for item in second_page["items"]] == ["Old inbox"]
    assert second_page["has_more"] is False

    sent = mailbox_store.list_mailbox(db_path=db_path, mailbox="发件箱")
    assert [item["message_id"] for item in sent["items"]] == [str(outbound_id)]
    assert sent["items"][0]["mailbox"] == "sent"

    inbox = mailbox_store.list_mailbox(db_path=db_path, mailbox="收件箱")
    assert [item["message_id"] for item in inbox["items"]] == ["email-mid", "email-old"]


def test_mcp_history_tools_use_mailbox_store(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(
            app.SETTINGS,
            bridge_db=db_path,
            attachment_dir=tmp_path / "attachments",
        ),
    )
    monkeypatch.setattr(resend_mcp_server, "STATE_DB_FILE", db_path)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    app.init_db()
    app.record_inbound_email(
        svix_id="svix-1",
        event={"type": "email.received", "data": {"email_id": "email-1"}},
        email={
            "id": "email-1",
            "from": "sender@example.com",
            "to": ["mail@example.com"],
            "subject": "Hello MCP",
            "text": "Searchable body",
        },
        attachments=[],
        addressed_to_inbound=False,
    )

    labeled = asyncio.run(
        resend_mcp_server.manage_email_labels(
            "email-1",
            kind="inbound",
            add_labels=["inbox"],
        )
    )
    searched = asyncio.run(resend_mcp_server.search_emails(label="inbox"))
    listed = asyncio.run(resend_mcp_server.list_emails(mailbox="收件箱", limit=5))
    viewed = asyncio.run(resend_mcp_server.view_email("email-1", kind="inbound"))

    assert labeled["labels"] == ["inbox"]
    assert searched["items"][0]["subject"] == "Hello MCP"
    assert listed["mailbox"] == "inbox"
    assert listed["items"][0]["subject"] == "Hello MCP"
    assert "收件箱" in listed["display"]
    assert viewed["text_body"] == "Searchable body"


def test_mcp_tools_reject_automated_tool_source(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "tool")

    with pytest.raises(PermissionError, match="disabled for automated tool sessions"):
        asyncio.run(resend_mcp_server.search_emails())
    with pytest.raises(PermissionError, match="disabled for automated tool sessions"):
        asyncio.run(resend_mcp_server.list_emails())


def test_manage_cli_status_uses_db_health(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(app, "SETTINGS", replace(app.SETTINGS, bridge_db=tmp_path / "state.db"))
    app.init_db()

    manage.command_status(None)

    output = capsys.readouterr().out
    assert '"ok": true' in output


def test_manage_install_mcp_loads_project_env(monkeypatch, tmp_path, capsys):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(manage.bridge_settings, "hermes_home", lambda: hermes_home)
    monkeypatch.delenv("RESEND_BRIDGE_URL", raising=False)

    def fake_load_project_env():
        monkeypatch.setenv("RESEND_BRIDGE_URL", "http://127.0.0.1:9999/")

    monkeypatch.setattr(manage.bridge_settings, "load_project_env", fake_load_project_env)

    manage.command_install_mcp(None)

    capsys.readouterr()
    config = manage.yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["mcp_servers"]["resend_email"]["env"]["RESEND_BRIDGE_URL"] == (
        "http://127.0.0.1:9999"
    )
    assert config["mcp_servers"]["resend_email"]["timeout"] == 120


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


def test_attachment_download_failure_records_error_and_continues(monkeypatch, tmp_path):
    records = []
    monkeypatch.setattr(
        app,
        "SETTINGS",
        replace(app.SETTINGS, attachment_dir=tmp_path / "attachments"),
    )
    monkeypatch.setattr(
        app,
        "record_attachment_history",
        lambda **kwargs: records.append(kwargs),
    )

    class BrokenStream:
        async def __aenter__(self):
            raise RuntimeError("download failed")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakeClient:
        def stream(self, *_args, **_kwargs):
            return BrokenStream()

    results = asyncio.run(
        app.download_relevant_attachments(
            FakeClient(),
            "email-1",
            [
                {
                    "id": "att-1",
                    "filename": "report.txt",
                    "content_type": "text/plain",
                    "download_url": "https://example.com/report.txt",
                }
            ],
        )
    )

    assert len(results) == 1
    assert results[0]["relevant"] is True
    assert results[0]["error"] == "download failed"
    assert records[0]["item"]["error"] == "download failed"
