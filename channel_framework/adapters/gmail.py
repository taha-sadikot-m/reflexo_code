"""Gmail channel adapter."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from channel_framework.adapters.base import (
    ChannelAdapter,
    NormalizedMessage,
    WebhookSubscription,
)
from channel_framework.credentials import get_valid_tokens
from shared.db import tenant_connection


def _decode(data: str) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", errors="replace"
    )


def _headers(message: dict[str, Any]) -> dict[str, str]:
    values = (message.get("payload") or {}).get("headers") or []
    return {str(h.get("name") or "").lower(): str(h.get("value") or "") for h in values}


class GmailAdapter(ChannelAdapter):
    platform = "gmail"

    async def setup_notifications(
        self, business_id: str, access_token: str
    ) -> WebhookSubscription:
        topic = os.environ["GOOGLE_PUBSUB_TOPIC"]
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/watch",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"topicName": topic, "labelIds": ["INBOX"]},
            )
            response.raise_for_status()
            data = response.json()
        expiration = data.get("expiration")
        return WebhookSubscription(
            subscription_id=str(data.get("historyId") or f"gmail:{business_id}"),
            endpoint_url="/webhooks/channel/gmail/pubsub",
            expires_at=(
                datetime.fromtimestamp(
                    int(expiration) / 1000,
                    tz=timezone.utc,
                ).isoformat()
                if expiration
                else None
            ),
            renewal_interval_hours=168,
        )

    async def parse_webhook(
        self, raw_payload: dict[str, Any], business_id: str
    ) -> NormalizedMessage | None:
        message = raw_payload.get("gmail_message")
        if not isinstance(message, dict):
            envelope = raw_payload.get("message") or {}
            encoded = envelope.get("data") if isinstance(envelope, dict) else None
            if encoded:
                notice = json.loads(_decode(str(encoded)))
                history_id = str(notice.get("historyId") or "")
                credentials = await get_valid_tokens(business_id, self.platform)
                if not history_id or not credentials:
                    return None
                async with tenant_connection(business_id) as conn:
                    start_history_id = await conn.fetchval(
                        """
                        SELECT COALESCE(
                            platform_state->>'last_history_id',
                            subscription_id
                        )
                        FROM webhook_subscriptions
                        WHERE business_id = $1::uuid AND platform = 'gmail'
                        """,
                        business_id,
                    )
                if not start_history_id:
                    return None
                async with httpx.AsyncClient(timeout=30.0) as client:
                    history = await client.get(
                        "https://gmail.googleapis.com/gmail/v1/users/me/history",
                        headers={
                            "Authorization": f"Bearer {credentials['access_token']}"
                        },
                        params={
                            "startHistoryId": str(start_history_id),
                            "historyTypes": "messageAdded",
                            "labelId": "INBOX",
                        },
                    )
                    history.raise_for_status()
                    additions = [
                        item.get("message")
                        for row in history.json().get("history") or []
                        for item in row.get("messagesAdded") or []
                        if item.get("message")
                    ]
                    if not additions:
                        return None
                    fetched = await client.get(
                        "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                        + str(additions[-1]["id"]),
                        headers={
                            "Authorization": f"Bearer {credentials['access_token']}"
                        },
                        params={"format": "full"},
                    )
                    fetched.raise_for_status()
                    message = fetched.json()
                async with tenant_connection(business_id) as conn:
                    await conn.execute(
                        """
                        UPDATE webhook_subscriptions
                        SET platform_state = jsonb_set(
                                platform_state,
                                '{last_history_id}',
                                to_jsonb($2::text)
                            ),
                            last_event_at = now(),
                            updated_at = now()
                        WHERE business_id = $1::uuid AND platform = 'gmail'
                        """,
                        business_id,
                        history_id,
                    )
        if not isinstance(message, dict) or not message.get("id"):
            return None
        headers = _headers(message)
        sender = headers.get("from", "")
        sender_id = (
            sender.rsplit("<", 1)[-1].rstrip(">").strip().casefold()
            if "<" in sender
            else sender.strip().casefold()
        )
        payload = message.get("payload") or {}
        body = _decode((payload.get("body") or {}).get("data") or "")
        if not body:
            for part in payload.get("parts") or []:
                if part.get("mimeType") == "text/plain":
                    body = _decode((part.get("body") or {}).get("data") or "")
                    break
        return NormalizedMessage(
            channel=self.platform,
            business_id=business_id,
            sender_id=sender_id,
            sender_display=sender,
            content=body,
            content_html=None,
            subject=headers.get("subject"),
            thread_id=str(message.get("threadId") or "") or None,
            platform_message_id=str(message["id"]),
            metadata={"label_ids": message.get("labelIds") or []},
        )

    def get_outbound_tool(self) -> str:
        return "send_email"

    async def teardown_notifications(self, business_id: str) -> None:
        credentials = await get_valid_tokens(business_id, self.platform)
        if not credentials:
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/stop",
                headers={
                    "Authorization": f"Bearer {credentials['access_token']}"
                },
            )
            response.raise_for_status()
