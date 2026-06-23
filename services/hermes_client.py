from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from typing import Any

import app as bridge_app
from db.state import OutboundStatus, StepStatus
from services.resend_outbound import HermesDecision
from settings import APP_DIR


@lru_cache(maxsize=8)
def load_prompt_template(name: str) -> str:
    path = APP_DIR / "prompts" / name
    return path.read_text(encoding="utf-8")


def _generated_root_text() -> str:
    roots = bridge_app.GENERATED_ATTACHMENT_ROOTS
    root_texts = [str(path) for path in roots]
    return "、".join(root_texts) or "生成文件目录"


def _build_hermes_task_instruction(prompt_record: dict[str, Any]) -> str:
    inbound_address = bridge_app.SETTINGS.inbound_address
    return load_prompt_template("hermes_email_task.md").format(
        inbound_address=inbound_address,
        generated_root_text=_generated_root_text(),
        prompt_record_json=json.dumps(prompt_record, ensure_ascii=False, indent=2),
    )


def build_hermes_task_prompt(prompt_record: dict[str, Any]) -> str:
    return _build_hermes_task_instruction(prompt_record)


def parse_json_decision(content: str) -> dict[str, Any]:
    original_content = content.strip()
    content = strip_json_code_fence(original_content)
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                result = parse_loose_decision_object(match.group(0), str(exc))
        else:
            result = fallback_notify_decision(
                original_content,
                "Hermes did not return a JSON object.",
            )
    if not isinstance(result, dict):
        return fallback_notify_decision(
            original_content,
            "Hermes decision was not a JSON object.",
        )
    action = str(result.get("action", "notify")).lower()
    if action not in {"reply", "notify"}:
        result["action"] = "notify"
    else:
        result["action"] = action
    result.setdefault("reply_subject", "")
    result.setdefault("reply_text", "")
    result.setdefault("reply_attachments", [])
    result.setdefault("owner_report_attachments", [])
    result.pop("forward_received_attachments", None)
    if result.get("reply_attachments") in (None, ""):
        result["reply_attachments"] = []
    elif not isinstance(result.get("reply_attachments"), list):
        result["reply_attachments"] = bridge_app.ensure_list(result.get("reply_attachments"))
    if result.get("owner_report_attachments") in (None, ""):
        result["owner_report_attachments"] = []
    elif not isinstance(result.get("owner_report_attachments"), list):
        result["owner_report_attachments"] = bridge_app.ensure_list(
            result.get("owner_report_attachments")
        )
    result.setdefault("owner_report", "")
    result["executed_task"] = coerce_bool(result.get("executed_task"))
    normalized = HermesDecision.model_validate(result).model_dump()
    normalized.update({key: value for key, value in result.items() if key not in normalized})
    return normalized


def strip_json_code_fence(content: str) -> str:
    content = content.strip()
    if not content.startswith("```"):
        return content
    lines = content.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_loose_decision_object(content: str, parse_error: str) -> dict[str, Any]:
    keys = (
        "action",
        "executed_task",
        "owner_report",
        "owner_report_attachments",
        "reply_subject",
        "reply_text",
        "reply_attachments",
    )
    pattern = r'"(' + "|".join(re.escape(key) for key in keys) + r')"\s*:'
    matches = list(re.finditer(pattern, content))
    values: dict[str, str] = {}

    for index, match in enumerate(matches):
        key = match.group(1)
        value_start = match.end()
        value_end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else content.rfind("}")
        )
        raw_value = content[value_start:value_end].strip().rstrip(",").strip()
        values[key] = parse_loose_string_value(raw_value)

    if not values:
        return fallback_notify_decision(content, parse_error)

    values.setdefault("action", "notify")
    values.setdefault("reply_subject", "")
    values.setdefault("reply_text", "")
    values.setdefault("reply_attachments", [])
    values.setdefault("owner_report", "Hermes returned malformed JSON.")
    values["_parse_warning"] = parse_error
    return values


def parse_loose_string_value(raw_value: str) -> str:
    raw_value = raw_value.strip()
    try:
        parsed = json.loads(raw_value)
        return str(parsed)
    except json.JSONDecodeError:
        pass
    if raw_value.startswith('"') and raw_value.endswith('"'):
        raw_value = raw_value[1:-1]
    elif raw_value.startswith('"'):
        raw_value = raw_value[1:]
    elif raw_value.endswith('"'):
        raw_value = raw_value[:-1]
    return decode_common_json_escapes(raw_value)


def decode_common_json_escapes(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\/", "/")
        .replace("\\\\", "\\")
    )


def fallback_notify_decision(content: str, reason: str) -> dict[str, Any]:
    summary = content.strip() or "Hermes returned an empty decision."
    return {
        "action": "notify",
        "reply_subject": "",
        "reply_text": "",
        "reply_attachments": [],
        "owner_report_attachments": [],
        "owner_report": summary[:3000],
        "executed_task": False,
        "_parse_warning": reason,
    }


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "已执行"}
    return False


async def run_hermes_task(
    prompt_record: dict[str, Any],
    email_id: str,
    subject: str,
) -> dict[str, Any]:
    prompt_text = build_hermes_task_prompt(prompt_record)
    outbound_id = bridge_app.create_outbound_message(
        kind="hermes_task",
        email_id=email_id,
        recipient="hermes-direct",
        subject=subject,
        body_text=prompt_text,
        payload={"mode": "direct_subprocess"},
    )
    stdout_text = ""
    stderr_text = ""
    try:
        process = await asyncio.create_subprocess_exec(
            str(bridge_app.SETTINGS.hermes_send_bin),
            "chat",
            "--query",
            prompt_text,
            "--quiet",
            "--source",
            "tool",
            "--yolo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=bridge_app.SETTINGS.hermes_timeout_seconds,
        )
        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        if process.returncode != 0:
            raise RuntimeError(
                f"Hermes task failed with exit code {process.returncode}: {stderr_text}"
            )
        if not stdout_text.strip():
            raise RuntimeError("Hermes CLI task returned empty content")
        decision = parse_json_decision(stdout_text)
    except Exception as exc:
        error_message = bridge_app.exception_message(exc)
        bridge_app.update_outbound_message(
            outbound_id,
            status=OutboundStatus.FAILED,
            stdout=stdout_text,
            stderr=stderr_text,
            error=error_message[:1000],
        )
        bridge_app.record_hermes_decision(
            email_id=email_id,
            prompt=prompt_record,
            response_content=stdout_text or None,
            error=error_message[:1000],
        )
        bridge_app.record_processing_step(
            step="hermes_task",
            status=StepStatus.FAILED,
            email_id=email_id,
            error=error_message[:1000],
            detail={"mode": "direct_subprocess"},
        )
        decision = fallback_notify_decision(
            stdout_text,
            f"Hermes task execution failed: {error_message}",
        )
        decision["owner_report"] = (
            "Hermes 执行邮件任务时失败，已把失败原因记录到桥接服务日志。"
        )
        decision["executed_task"] = False
        return decision

    bridge_app.update_outbound_message(
        outbound_id,
        status=OutboundStatus.DONE,
        stdout=stdout_text,
        stderr=stderr_text,
    )
    bridge_app.record_hermes_decision(
        email_id=email_id,
        prompt=prompt_record,
        response_content=stdout_text,
        decision=decision,
    )
    bridge_app.record_processing_step(
        step="hermes_task",
        status=StepStatus.DONE,
        email_id=email_id,
        detail={
            "mode": "direct_subprocess",
            "action": decision.get("action"),
            "executed_task": coerce_bool(decision.get("executed_task")),
        },
    )
    return decision


async def run_hermes_email_task(
    email: dict[str, Any],
    attachments: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    email_id: str,
) -> dict[str, Any]:
    from email.utils import parseaddr

    sender = parseaddr(str(email.get("from") or ""))[1] or str(email.get("from") or "")
    prompt_record = {
        "task": "Decide and execute the actionable task requested by this inbound bot email.",
        "sender": sender,
        "email": bridge_app.email_summary(email),
        "attachments": attachments,
        "downloaded_files": downloaded,
    }
    return await run_hermes_task(prompt_record, email_id, str(email.get("subject") or ""))
