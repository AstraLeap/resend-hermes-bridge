from __future__ import annotations

import os

_DEFAULT_LANGUAGE = "zh"
_SUPPORTED_LANGUAGES = {"zh", "en"}


def _get_language() -> str:
    """Return the active language code from ``BRIDGE_LANGUAGE``.

    Defaults to Chinese so existing installations and tests keep their
    current behavior. Unknown values fall back to the default.
    """
    lang = (os.getenv("BRIDGE_LANGUAGE") or _DEFAULT_LANGUAGE).strip().lower()
    return lang if lang in _SUPPORTED_LANGUAGES else _DEFAULT_LANGUAGE


class _LocalizedString:
    """Descriptor that returns the translation for the active language."""

    def __init__(self, key: str) -> None:
        self.key = key

    def __get__(self, obj: object | None, type: type | None = None) -> str:
        return _STRINGS[_get_language()][self.key]


# Translation tables. Keep placeholder names identical across languages so
# callers can ``.format(...)`` without knowing the active language.
_STRINGS = {
    "zh": {
        # NotificationTitles
        "notification.bot_email_received": "{ai_name}收到邮件啦！正在处理中哦~",
        "notification.non_bot_email": "主人，你有一封新邮件~",
        "notification.whitelist_forward": "{ai_name}收到一封不在白名单的邮件，转交给主人啦",
        "notification.auto_reply_sent": "{ai_name}已自动回复：",
        "notification.draft_confirmation": "请确认是否发送以下邮件：",
        # EmailLabels
        "email.body": "正文",
        "email.html_body": "HTML 正文",
        "email.empty_body": "(空)",
        "email.attachments": "附件",
        "email.field": "字段",
        "email.content": "内容",
        "email.file": "文件",
        "email.size": "大小",
        "email.inline_image": "内联图片",
        "email.more_attachments": "还有 {count} 个附件",
        "email.from": "发件人",
        "email.to": "收件人",
        "email.cc": "抄送",
        "email.bcc": "密送",
        "email.reply_to": "回复地址",
        "email.subject": "主题",
        "email.draft_id": "Draft ID",
        "email.email_id": "Email ID",
        # MailboxLabels
        "mailbox.all": "邮件列表",
        "mailbox.inbox": "收件箱",
        "mailbox.sent": "发件箱",
        "mailbox.trash": "回收站",
        "mailbox.no_emails_template": "{title}：没有邮件。",
        "mailbox.paging_template": "{title}：显示 {start}-{end} / {total}，{sort_desc}。",
        "mailbox.sort_desc": "按最新时间排序",
        "mailbox.continue_offset_template": "继续查看请使用 offset={offset}, limit={limit}。",
        "mailbox.direction_in": "收",
        "mailbox.direction_out": "发",
        "mailbox.deleted_suffix": " 已删除",
        "mailbox.no_subject": "(无主题)",
        # ProcessingMessages
        "processing.activity_summary_prefix": "**任务总结：**",
        "processing.result_section": "**处理结果**",
        "processing.reply_skipped_no_body": (
            "Hermes 选择了回复，但没有提供回复正文，因此桥接服务跳过了邮件回复。"
        ),
        "processing.reply_footer": "Resend ID: `{reply_id}`",
        "processing.truncated": "\n...[已截断]",
        # HermesMessages
        "hermes.task_failed": "Hermes 执行邮件任务时失败，已把失败原因记录到桥接服务日志。",
        "hermes.malformed_json": "Hermes 返回了格式错误的 JSON。",
        "hermes.empty_decision": "Hermes 返回了空决策。",
        "hermes.no_json_object": "Hermes 没有返回 JSON 对象。",
        "hermes.decision_not_object": "Hermes 的决策不是 JSON 对象。",
        # McpMessages
        "mcp.sent_notification": "邮件已通过 Resend 发送。",
        "mcp.resend_id_prefix": "Resend ID: `{resend_id}`",
        "mcp.draft_footer": "确认后我会发送 Draft ID `{draft_id}`。",
        "mcp.skip_attachment_prefix": "自动回复时跳过了无效附件：",
        "mcp.skip_attachment_separator": "；",
        "mcp.outside_generated_dirs": "位于生成的附件目录之外",
        "mcp.not_found": "未找到",
        "mcp.downloaded_not_found": "下载的文件未找到",
        # ReplyMessages
        "reply.default_reply_subject": "Re: 你的邮件",
        "reply.re_prefix": "Re: ",
        # ErrorMessages
        "error.inbound_processing_failed": (
            "Resend 入站处理失败。\n邮件 ID: {email_id}\n错误: {error}"
        ),
    },
    "en": {
        # NotificationTitles
        "notification.bot_email_received": "{ai_name} received an email and is processing it~",
        "notification.non_bot_email": "You have a new email~",
        "notification.whitelist_forward": (
            "{ai_name} received an email not in the allowlist and forwarded it to you"
        ),
        "notification.auto_reply_sent": "{ai_name} auto-replied:",
        "notification.draft_confirmation": "Please confirm whether to send the following email:",
        # EmailLabels
        "email.body": "Body",
        "email.html_body": "HTML Body",
        "email.empty_body": "(empty)",
        "email.attachments": "Attachments",
        "email.field": "Field",
        "email.content": "Content",
        "email.file": "File",
        "email.size": "Size",
        "email.inline_image": "Inline image",
        "email.more_attachments": "{count} more attachments",
        "email.from": "From",
        "email.to": "To",
        "email.cc": "CC",
        "email.bcc": "BCC",
        "email.reply_to": "Reply-To",
        "email.subject": "Subject",
        "email.draft_id": "Draft ID",
        "email.email_id": "Email ID",
        # MailboxLabels
        "mailbox.all": "All Mail",
        "mailbox.inbox": "Inbox",
        "mailbox.sent": "Sent",
        "mailbox.trash": "Trash",
        "mailbox.no_emails_template": "{title}: no emails.",
        "mailbox.paging_template": "{title}: showing {start}-{end} / {total}, {sort_desc}.",
        "mailbox.sort_desc": "sorted by newest first",
        "mailbox.continue_offset_template": "Use offset={offset}, limit={limit} to see more.",
        "mailbox.direction_in": "IN",
        "mailbox.direction_out": "OUT",
        "mailbox.deleted_suffix": " deleted",
        "mailbox.no_subject": "(no subject)",
        # ProcessingMessages
        "processing.activity_summary_prefix": "**Activity summary:**",
        "processing.result_section": "**Result**",
        "processing.reply_skipped_no_body": (
            "Hermes chose to reply but did not provide a reply body, "
            "so the bridge skipped the email reply."
        ),
        "processing.reply_footer": "Resend ID: `{reply_id}`",
        "processing.truncated": "\n...[truncated]",
        # HermesMessages
        "hermes.task_failed": (
            "Hermes failed while executing the email task; "
            "the failure reason has been logged by the bridge."
        ),
        "hermes.malformed_json": "Hermes returned malformed JSON.",
        "hermes.empty_decision": "Hermes returned an empty decision.",
        "hermes.no_json_object": "Hermes did not return a JSON object.",
        "hermes.decision_not_object": "Hermes decision was not a JSON object.",
        # McpMessages
        "mcp.sent_notification": "Email sent via Resend.",
        "mcp.resend_id_prefix": "Resend ID: `{resend_id}`",
        "mcp.draft_footer": "After confirmation I will send Draft ID `{draft_id}`.",
        "mcp.skip_attachment_prefix": "Skipped invalid attachments for auto-reply: ",
        "mcp.skip_attachment_separator": "; ",
        "mcp.outside_generated_dirs": "outside generated attachment directories",
        "mcp.not_found": "not found",
        "mcp.downloaded_not_found": "downloaded file not found",
        # ReplyMessages
        "reply.default_reply_subject": "Re: your email",
        "reply.re_prefix": "Re: ",
        # ErrorMessages
        "error.inbound_processing_failed": (
            "Resend inbound processing failed.\nEmail ID: {email_id}\nError: {error}"
        ),
    },
}


class NotificationTitles:
    """Titles used for owner-facing email notifications."""

    BOT_EMAIL_RECEIVED = _LocalizedString("notification.bot_email_received")
    NON_BOT_EMAIL = _LocalizedString("notification.non_bot_email")
    WHITELIST_FORWARD = _LocalizedString("notification.whitelist_forward")
    AUTO_REPLY_SENT = _LocalizedString("notification.auto_reply_sent")
    DRAFT_CONFIRMATION = _LocalizedString("notification.draft_confirmation")


class EmailLabels:
    """Labels used when rendering emails as Markdown for the owner."""

    BODY = _LocalizedString("email.body")
    HTML_BODY = _LocalizedString("email.html_body")
    EMPTY_BODY = _LocalizedString("email.empty_body")
    ATTACHMENTS = _LocalizedString("email.attachments")
    FIELD = _LocalizedString("email.field")
    CONTENT = _LocalizedString("email.content")
    FILE = _LocalizedString("email.file")
    SIZE = _LocalizedString("email.size")
    INLINE_IMAGE = _LocalizedString("email.inline_image")
    MORE_ATTACHMENTS = _LocalizedString("email.more_attachments")
    FROM = _LocalizedString("email.from")
    TO = _LocalizedString("email.to")
    CC = _LocalizedString("email.cc")
    BCC = _LocalizedString("email.bcc")
    REPLY_TO = _LocalizedString("email.reply_to")
    SUBJECT = _LocalizedString("email.subject")
    DRAFT_ID = _LocalizedString("email.draft_id")
    EMAIL_ID = _LocalizedString("email.email_id")


class MailboxLabels:
    """User-facing mailbox listing strings."""

    ALL = _LocalizedString("mailbox.all")
    INBOX = _LocalizedString("mailbox.inbox")
    SENT = _LocalizedString("mailbox.sent")
    TRASH = _LocalizedString("mailbox.trash")
    NO_EMAILS_TEMPLATE = _LocalizedString("mailbox.no_emails_template")
    PAGING_TEMPLATE = _LocalizedString("mailbox.paging_template")
    SORT_DESC = _LocalizedString("mailbox.sort_desc")
    CONTINUE_OFFSET_TEMPLATE = _LocalizedString("mailbox.continue_offset_template")
    DIRECTION_IN = _LocalizedString("mailbox.direction_in")
    DIRECTION_OUT = _LocalizedString("mailbox.direction_out")
    DELETED_SUFFIX = _LocalizedString("mailbox.deleted_suffix")
    NO_SUBJECT = _LocalizedString("mailbox.no_subject")


class ProcessingMessages:
    """Strings related to processing results and activity summaries."""

    ACTIVITY_SUMMARY_PREFIX = _LocalizedString("processing.activity_summary_prefix")
    RESULT_SECTION = _LocalizedString("processing.result_section")
    REPLY_SKIPPED_NO_BODY = _LocalizedString("processing.reply_skipped_no_body")
    REPLY_FOOTER = _LocalizedString("processing.reply_footer")
    TRUNCATED = _LocalizedString("processing.truncated")


class HermesMessages:
    """User-facing messages coming from Hermes task handling."""

    TASK_FAILED = _LocalizedString("hermes.task_failed")
    MALFORMED_JSON = _LocalizedString("hermes.malformed_json")
    EMPTY_DECISION = _LocalizedString("hermes.empty_decision")
    NO_JSON_OBJECT = _LocalizedString("hermes.no_json_object")
    DECISION_NOT_OBJECT = _LocalizedString("hermes.decision_not_object")


class McpMessages:
    """User-facing strings from the MCP server and draft flow."""

    SENT_NOTIFICATION = _LocalizedString("mcp.sent_notification")
    RESEND_ID_PREFIX = _LocalizedString("mcp.resend_id_prefix")
    DRAFT_FOOTER = _LocalizedString("mcp.draft_footer")
    SKIP_ATTACHMENT_PREFIX = _LocalizedString("mcp.skip_attachment_prefix")
    SKIP_ATTACHMENT_SEPARATOR = _LocalizedString("mcp.skip_attachment_separator")
    OUTSIDE_GENERATED_DIRS = _LocalizedString("mcp.outside_generated_dirs")
    NOT_FOUND = _LocalizedString("mcp.not_found")
    DOWNLOADED_NOT_FOUND = _LocalizedString("mcp.downloaded_not_found")


class ReplyMessages:
    """Strings used when building automatic email replies."""

    DEFAULT_REPLY_SUBJECT = _LocalizedString("reply.default_reply_subject")
    RE_PREFIX = _LocalizedString("reply.re_prefix")


class ErrorMessages:
    """User-facing error messages produced by the bridge."""

    INBOUND_PROCESSING_FAILED = _LocalizedString("error.inbound_processing_failed")
