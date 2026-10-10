"""Microsoft Graph Outlook adapter."""

from __future__ import annotations

import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from channel_framework.adapters.base import (
    ChannelAdapter,
    NormalizedMessage,
    WebhookSubscription,
)
from channel_framework.credentials import get_valid_tokens


class OutlookAdapter(ChannelAdapter):
    platform = "outlook"

    async def setup_notifications(
        self, business_id: str, access_token: str
    ) -> WebhookSubscription:
        base = os.environ["PUBLIC_API_URL"].rstrip("/")
        client_state = self._client_state(business_id)
        expiration = datetime.now(timezone.utc) + timedelta(hours=47)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://graph.microsoft.com/v1.0/subscriptions",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "changeType": "created",
                    "notificationUrl": f"{base}/webhooks/channel/outlook/{business_id}",
                    "resource": "me/mailFolders('Inbox')/messages",
                    "expirationDateTime": expiration.isoformat(),
                    "clientState": client_state,
                },
            )
            response.raise_for_status()
            data = response.json()
        return WebhookSubscription(
            subscription_id=str(data["id"]),
            endpoint_url=f"{base}/webhooks/channel/outlook/{business_id}",
            expires_at=str(data.get("expirationDateTime") or expiration.isoformat()),
            renewal_interval_hours=48,
        )

    @staticmethod
    def _client_state(business_id: str) -> str:
        secret = os.environ["OUTLOOK_WEBHOOK_CLIENT_STATE"].encode()
        return hmac.new(secret, business_id.encode(), hashlib.sha256).hexdigest()

    async def parse_webhook(
        self, raw_payload: dict[str, Any], business_id: str
    ) -> NormalizedMessage | None:
        notifications = raw_payload.get("value") or []
        if not notifications:
            return None
        notification = notifications[0]
        try:
            expected = self._client_state(business_id)
        except KeyError:
            return None
        if not expected or notification.get("clientState") != expected:
            return None
        resource = str(notification.get("resource") or "")
        message_id = resource.rsplit("/", 1)[-1]
        credentials = await get_valid_tokens(business_id, self.platform)
        if not message_id or not credentials:
            return None
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{message_id}",
                headers={
                    "Authorization": f"Bearer {credentials['access_token']}"
                },
            )
            response.raise_for_status()
            message = response.json()
        if not isinstance(message, dict) or not message.get("id"):
            return None
        sender = ((message.get("from") or {}).get("emailAddress") or {})
        body = message.get("body") or {}
        return NormalizedMessage(
            channel=self.platform,
            business_id=business_id,
            sender_id=str(sender.get("address") or "").casefold(),
            sender_display=str(sender.get("name") or sender.get("address") or ""),
            content=str(message.get("bodyPreview") or body.get("content") or ""),
            content_html=(
                str(body.get("content") or "")
                if str(body.get("contentType") or "").lower() == "html"
                else None
            ),
            subject=str(message.get("subject") or "") or None,
            thread_id=str(message.get("conversationId") or "") or None,
            platform_message_id=str(message["id"]),
            metadata={"importance": message.get("importance")},
        )

    def get_outbound_tool(self) -> str:
        return "send_outlook_email"

    async def teardown_notifications(self, business_id: str) -> None:
        credentials = await get_valid_tokens(business_id, self.platform)
        if not credentials:
            return
        from shared.db import tenant_connection

        async with tenant_connection(business_id) as conn:
            subscription_id = await conn.fetchval(
                """
                SELECT subscription_id FROM webhook_subscriptions
                WHERE business_id = $1::uuid AND platform = 'outlook'
                """,
                business_id,
            )
        if not subscription_id:
            return
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.delete(
                f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}",
                headers={
                    "Authorization": f"Bearer {credentials['access_token']}"
                },
            )
            if response.status_code != 404:
                response.raise_for_status()
