"""Direct Slack Web API send."""

from __future__ import annotations

from typing import Any

import httpx

from channel_framework.credentials import get_valid_tokens


async def send_slack_message(
    *,
    business_id: str,
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    credentials = await get_valid_tokens(business_id, "slack")
    if not credentials:
        raise RuntimeError("slack is not connected")
    payload: dict[str, Any] = {"channel": channel_id, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {credentials['access_token']}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "Slack send failed"))
        return {"status": "sent", "message_id": data.get("ts")}
