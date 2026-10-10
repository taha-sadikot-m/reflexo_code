"""Slack Events API adapter."""

from __future__ import annotations

from typing import Any

from channel_framework.adapters.base import (
    ChannelAdapter,
    NormalizedMessage,
    WebhookSubscription,
)
from shared.db import tenant_connection


async def _is_connected_team(business_id: str, team_id: str) -> bool:
    async with tenant_connection(business_id) as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT 1 FROM connected_accounts
                WHERE business_id = $1::uuid
                  AND platform = 'slack'
                  AND account_identifier = $2
                  AND is_active = true
                """,
                business_id,
                team_id,
            )
        )


class SlackAdapter(ChannelAdapter):
    platform = "slack"

    async def setup_notifications(
        self, business_id: str, access_token: str
    ) -> WebhookSubscription:
        del access_token
        return WebhookSubscription(
            subscription_id=f"slack:{business_id}",
            endpoint_url=f"/webhooks/channel/slack/{business_id}",
        )

    async def parse_webhook(
        self, raw_payload: dict[str, Any], business_id: str
    ) -> NormalizedMessage | None:
        event = raw_payload.get("event") or {}
        if (
            event.get("type") != "message"
            or event.get("subtype")
            or event.get("bot_id")
        ):
            return None
        team_id = str(raw_payload.get("team_id") or "")
        if not await _is_connected_team(business_id, team_id):
            return None
        event_id = raw_payload.get("event_id") or event.get("client_msg_id") or event.get("ts")
        if not event_id:
            return None
        return NormalizedMessage(
            channel=self.platform,
            business_id=business_id,
            sender_id=str(event.get("user") or ""),
            sender_display=str(event.get("user_name") or event.get("user") or ""),
            content=str(event.get("text") or ""),
            thread_id=str(event.get("thread_ts") or event.get("ts") or "") or None,
            platform_message_id=str(event_id),
            attachments=list(event.get("files") or []),
            metadata={
                "channel_id": event.get("channel"),
                "team_id": team_id,
            },
        )

    def get_outbound_tool(self) -> str:
        return "send_slack_message"

    async def teardown_notifications(self, business_id: str) -> None:
        del business_id
