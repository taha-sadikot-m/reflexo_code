"""Direct Fast2SMS client using per-business API keys."""

from __future__ import annotations

from typing import Any

import httpx

from channel_framework.credentials import load_tokens


async def send_sms(
    business_id: str,
    *,
    phone: str,
    message: str,
    route: str = "q",
) -> dict[str, Any]:
    credentials = await load_tokens(business_id, "fast2sms")
    if not credentials:
        raise RuntimeError("Fast2SMS is not connected")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://www.fast2sms.com/dev/bulkV2",
            headers={"authorization": credentials["access_token"]},
            data={
                "route": route,
                "message": message,
                "numbers": phone.lstrip("+"),
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("return") is False:
            raise RuntimeError(str(data.get("message") or "Fast2SMS send failed"))
        return data
