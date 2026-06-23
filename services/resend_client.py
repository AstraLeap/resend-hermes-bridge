from __future__ import annotations

from typing import Any

import httpx

RESEND_BASE_URL = "https://api.resend.com"


class ResendAPIError(RuntimeError):
    def __init__(self, message: str, *, response_body: Any):
        super().__init__(message)
        self.response_body = response_body


def resend_headers(*, api_key: str, user_agent: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }


async def fetch_received_email(
    client: httpx.AsyncClient,
    email_id: str,
    *,
    api_key: str,
    user_agent: str,
) -> dict[str, Any]:
    response = await client.get(
        f"{RESEND_BASE_URL}/emails/receiving/{email_id}",
        params={"html_format": "cid"},
        headers=resend_headers(api_key=api_key, user_agent=user_agent),
    )
    response.raise_for_status()
    return response.json()


async def fetch_received_attachments(
    client: httpx.AsyncClient,
    email_id: str,
    *,
    api_key: str,
    user_agent: str,
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{RESEND_BASE_URL}/emails/receiving/{email_id}/attachments",
        headers=resend_headers(api_key=api_key, user_agent=user_agent),
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        return payload
    return list(payload.get("data") or [])


async def send_email(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    api_key: str,
    user_agent: str,
) -> dict[str, Any]:
    response = await client.post(
        f"{RESEND_BASE_URL}/emails",
        headers=resend_headers(api_key=api_key, user_agent=user_agent),
        json=payload,
    )
    try:
        response_body = response.json()
    except ValueError:
        response_body = {"text": response.text}
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ResendAPIError(str(exc), response_body=response_body) from exc
    return response_body if isinstance(response_body, dict) else {"data": response_body}
