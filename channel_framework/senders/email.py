"""Direct Gmail and Outlook email sends."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

import httpx

from channel_framework.credentials import get_valid_tokens


async def send_email(
    *,
    business_id: str,
    platform: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    credentials = await get_valid_tokens(business_id, platform)
    if not credentials:
        raise RuntimeError(f"{platform} is not connected")
    token = credentials["access_token"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        if platform == "gmail":
            message = EmailMessage()
            message["To"] = to
            message["Subject"] = subject
            message.set_content(body)
            payload: dict[str, Any] = {
                "raw": base64.urlsafe_b64encode(message.as_bytes()).decode()
            }
            if thread_id:
                payload["threadId"] = thread_id
            response = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        elif platform == "outlook":
            response = await client.post(
                "https://graph.microsoft.com/v1.0/me/sendMail",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "message": {
                        "subject": subject,
                        "body": {"contentType": "Text", "content": body},
                        "toRecipients": [
                            {"emailAddress": {"address": to}}
                        ],
                    },
                    "saveToSentItems": True,
                },
            )
        else:
            raise ValueError(f"Unsupported email platform: {platform}")
        response.raise_for_status()
        data = response.json() if response.content else {}
        return {"status": "sent", "message_id": data.get("id")}
