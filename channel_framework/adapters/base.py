"""Contracts implemented by every non-Meta communication adapter."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    channel: str
    business_id: str
    sender_id: str
    sender_display: str
    content: str
    platform_message_id: str
    content_html: str | None = None
    subject: str | None = None
    thread_id: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        for field_name in (
            "channel",
            "business_id",
            "sender_id",
            "platform_message_id",
        ):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"{field_name} is required")


@dataclass(frozen=True, slots=True)
class WebhookSubscription:
    subscription_id: str
    endpoint_url: str
    expires_at: str | None = None
    renewal_interval_hours: int | None = None


def subscription_expiry(value: str | None) -> datetime | None:
    """Providers report expiry as ISO text, but asyncpg binds timestamptz as datetime."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ChannelAdapter(ABC):
    platform: str

    @abstractmethod
    async def setup_notifications(
        self,
        business_id: str,
        access_token: str,
    ) -> WebhookSubscription:
        """Subscribe the provider to push notifications."""

    @abstractmethod
    async def parse_webhook(
        self,
        raw_payload: dict[str, Any],
        business_id: str,
    ) -> NormalizedMessage | None:
        """Normalize a provider webhook, or ignore non-message events."""

    @abstractmethod
    def get_outbound_tool(self) -> str:
        """Return the MCP send tool for this provider."""

    @abstractmethod
    async def teardown_notifications(self, business_id: str) -> None:
        """Remove the provider notification subscription."""

    async def validate_credentials(
        self,
        api_key: str,
        api_secret: str | None = None,
    ) -> bool:
        del api_key, api_secret
        return True

    async def renew_subscription(
        self,
        business_id: str,
        access_token: str,
    ) -> WebhookSubscription:
        return await self.setup_notifications(business_id, access_token)
