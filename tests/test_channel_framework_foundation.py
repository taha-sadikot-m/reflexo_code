"""Stage T10: Channel Framework foundation."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode()


def test_aes_gcm_round_trip_and_wrong_key_fails() -> None:
    from channel_framework.credentials import decrypt_secret, encrypt_secret

    encrypted = encrypt_secret(
        "refresh-token-secret",
        key=_key(1),
        associated_data="business:gmail",
    )
    assert b"refresh-token-secret" not in encrypted
    assert (
        decrypt_secret(
            encrypted,
            key=_key(1),
            associated_data="business:gmail",
        )
        == "refresh-token-secret"
    )
    with pytest.raises(InvalidTag):
        decrypt_secret(
            encrypted,
            key=_key(2),
            associated_data="business:gmail",
        )
    with pytest.raises(InvalidTag):
        decrypt_secret(
            encrypted,
            key=_key(1),
            associated_data="other:gmail",
        )


@pytest.mark.asyncio
async def test_adapter_contract_and_fake_normalization() -> None:
    from channel_framework.adapters.base import (
        ChannelAdapter,
        NormalizedMessage,
        WebhookSubscription,
    )

    with pytest.raises(TypeError):
        ChannelAdapter()

    class FakeAdapter(ChannelAdapter):
        platform = "gmail"

        async def setup_notifications(self, business_id, access_token):
            return WebhookSubscription("sub", "https://hooks.example.test")

        async def parse_webhook(self, raw_payload, business_id):
            return NormalizedMessage(
                channel=self.platform,
                business_id=business_id,
                sender_id=raw_payload["from"].casefold(),
                sender_display=raw_payload["name"],
                content=raw_payload["body"],
                subject=raw_payload["subject"],
                thread_id=raw_payload["thread_id"],
                platform_message_id=raw_payload["id"],
            )

        def get_outbound_tool(self):
            return "send_email"

        async def teardown_notifications(self, business_id):
            return None

    message = await FakeAdapter().parse_webhook(
        {
            "from": "CUSTOMER@EXAMPLE.COM",
            "name": "Customer",
            "body": "Need help",
            "subject": "Question",
            "thread_id": "thread-1",
            "id": "message-1",
        },
        "business-1",
    )
    assert isinstance(message, NormalizedMessage)
    assert message.sender_id == "customer@example.com"
    assert message.thread_id == "thread-1"


def test_framework_channels_are_recognized_by_identity_parser() -> None:
    from channel_framework.identity import normalize_channel_sender
    from identity.models import Channel

    assert Channel("gmail") is Channel.GMAIL
    assert normalize_channel_sender("gmail", " User@Example.COM ") == "user@example.com"
    assert normalize_channel_sender("slack", " U123 ") == "U123"


def test_channel_migration_contains_tenant_safe_foundation() -> None:
    migration = (
        Path(__file__).parents[1]
        / "db"
        / "migrations"
        / "029_channel_framework.sql"
    ).read_text(encoding="utf-8")
    for table in (
        "connected_accounts",
        "webhook_subscriptions",
        "channel_messages",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in migration
        assert f"CREATE POLICY {table}_tenant" in migration
    for channel in ("gmail", "outlook", "slack"):
        assert f"ADD VALUE IF NOT EXISTS '{channel}'" in migration
    assert "UNIQUE (business_id, channel, platform_message_id)" in migration
