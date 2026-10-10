"""Unit tests for Identity Resolution (mocked Redis/DB where needed)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from identity.channel_parsers import (
    normalize_phone,
    parse_instagram_dm_webhook,
    parse_whatsapp_webhook,
)
from identity.models import Channel, IdentityErrorCode, IdentityResolutionError
from identity.resolver import resolve_identity


class FakeCache:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_members: dict[str, set[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int = 3600):
        self.store[key] = value

    async def delete(self, key: str):
        self.store.pop(key, None)

    async def delete_pattern(self, pattern: str):
        pass

    async def sismember(self, key: str, member: str) -> bool:
        return member in self.set_members.get(key, set())

    async def sadd(self, key: str, *members: str):
        self.set_members.setdefault(key, set()).update(members)

    async def zadd(self, key: str, mapping: dict, ttl=None):
        z = self.zsets.setdefault(key, {})
        z.update(mapping)

    async def zcount(self, key: str, min_score: float, max_score: float) -> int:
        z = self.zsets.get(key, {})
        return sum(1 for s in z.values() if min_score <= s <= max_score)

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float):
        z = self.zsets.get(key, {})
        for m, s in list(z.items()):
            if min_score <= s <= max_score:
                del z[m]

    async def incr(self, key: str, ttl=None) -> int:
        val = int(self.store.get(key, "0")) + 1
        self.store[key] = str(val)
        return val


@pytest.mark.asyncio
async def test_missing_sender_id():
    with pytest.raises(IdentityResolutionError) as ei:
        await resolve_identity("whatsapp", "", "acct1")
    assert ei.value.code == IdentityErrorCode.MISSING_SENDER_ID


@pytest.mark.asyncio
async def test_invalid_channel():
    with pytest.raises(IdentityResolutionError) as ei:
        await resolve_identity("telegram", "+911", "acct1")
    assert ei.value.code == IdentityErrorCode.INVALID_CHANNEL


@pytest.mark.asyncio
async def test_unknown_channel_account():
    fake = FakeCache()
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)

    with (
        patch("identity.resolver.IdentityCache", return_value=fake),
        patch("identity.resolver.get_pool", return_value=pool),
    ):
        with pytest.raises(IdentityResolutionError) as ei:
            await resolve_identity("whatsapp", "+919876543210", "unknown_acct")
        assert ei.value.code == IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT


@pytest.mark.asyncio
async def test_resolve_owner_whatsapp():
    fake = FakeCache()
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(
        return_value={"business_id": "00000000-0000-0000-0000-000000000001"}
    )
    pool.fetchval = AsyncMock(return_value="+919999999999")

    with (
        patch("identity.resolver.IdentityCache", return_value=fake),
        patch("identity.resolver.get_pool", return_value=pool),
    ):
        result = await resolve_identity(
            "whatsapp",
            "+919999999999",
            "pnid_test",
            sender_display_name="Owner",
        )
    assert result.is_owner is True
    assert result.customer_id is None
    assert result.business_id == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_resolve_new_customer():
    fake = FakeCache()
    pool = AsyncMock()

    async def fetchrow(sql, *args):
        if "business_channels" in sql:
            return {"business_id": "biz-1"}
        if "identity_map" in sql and "JOIN customers" in sql:
            return None
        return None

    async def fetchval(sql, *args):
        if "owner_phone" in sql:
            return "+910000000000"
        return None

    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value="cust-new")
    conn.execute = AsyncMock()

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    pool.fetchval = AsyncMock(side_effect=fetchval)
    pool.execute = AsyncMock()

    with (
        patch("identity.resolver.IdentityCache", return_value=fake),
        patch("identity.resolver.get_pool", return_value=pool),
        patch("identity.bot_detection.IdentityCache", return_value=fake),
        patch("identity.bot_detection.get_pool", return_value=pool),
    ):
        result = await resolve_identity(
            "whatsapp",
            "+918888888888",
            "pnid_test",
            sender_display_name="Priya",
        )

    assert result.is_owner is False
    assert result.is_new_customer is True
    assert result.customer_id == "cust-new"
    assert result.customer_name == "Priya"


def test_normalize_phone():
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("919876543210") == "+919876543210"


def test_parse_whatsapp_webhook():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "107456789012345"},
                            "contacts": [
                                {"wa_id": "918765432109", "profile": {"name": "Priya"}}
                            ],
                            "messages": [
                                {
                                    "from": "918765432109",
                                    "id": "wamid.xxx",
                                    "timestamp": "1721300000",
                                    "type": "text",
                                    "text": {"body": "Hi"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    msgs = parse_whatsapp_webhook(payload)
    assert len(msgs) == 1
    assert msgs[0]["sender_external_id"] == "+918765432109"
    assert msgs[0]["receiving_account_id"] == "107456789012345"
    assert msgs[0]["sender_display_name"] == "Priya"
    assert msgs[0]["channel"] == Channel.WHATSAPP.value


def test_parse_instagram_dm_webhook():
    payload = {
        "entry": [
            {
                "id": "17841400123456",
                "messaging": [
                    {
                        "sender": {"id": "IGS9876543210"},
                        "message": {"mid": "m_xxx", "text": "How much?"},
                        "timestamp": 1,
                    }
                ],
            }
        ]
    }
    msgs = parse_instagram_dm_webhook(payload)
    assert msgs[0]["sender_external_id"] == "IGS9876543210"
    assert msgs[0]["receiving_account_id"] == "17841400123456"


@pytest.mark.asyncio
async def test_bot_known_number():
    from identity.bot_detection import BotDetector

    fake = FakeCache()
    await fake.sadd("reflexo:managed_numbers", "+919111111111")
    with patch("identity.bot_detection.IdentityCache", return_value=fake):
        detector = BotDetector()
        is_bot, layers = await detector.check("biz", "+919111111111", Channel.WHATSAPP)
    assert is_bot is True
    assert "known_number" in layers


@pytest.mark.asyncio
async def test_instagram_comment_resolves_business_via_sibling_dm():
    from identity.resolver import _resolve_business_id
    from shared.redis_keys import channel_account_key

    fake = FakeCache()
    pool = AsyncMock()

    async def fetchrow(_sql, *args):
        if args[0] == "instagram_comment":
            return None
        if args[0] == "instagram_dm":
            return {"business_id": "biz-sibling"}
        return None

    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    business_id = await _resolve_business_id(
        fake, pool, Channel.INSTAGRAM_COMMENT, "IG_ACCT"
    )
    assert business_id == "biz-sibling"
    assert fake.store[channel_account_key("instagram_comment", "IG_ACCT")] == "biz-sibling"


@pytest.mark.asyncio
async def test_facebook_comment_resolves_business_via_sibling_messenger():
    from identity.resolver import _resolve_business_id

    fake = FakeCache()
    pool = AsyncMock()

    async def fetchrow(_sql, *args):
        if args[0] == "facebook_comment":
            return None
        if args[0] == "facebook_messenger":
            return {"business_id": "biz-fb"}
        return None

    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    business_id = await _resolve_business_id(
        fake, pool, Channel.FACEBOOK_COMMENT, "PAGE_1"
    )
    assert business_id == "biz-fb"


@pytest.mark.asyncio
async def test_voice_resolves_business_via_whatsapp_row():
    """WhatsApp Calling reuses the WhatsApp phone number id, so there is no voice row."""
    from identity.resolver import _resolve_business_id
    from shared.redis_keys import channel_account_key

    fake = FakeCache()
    pool = AsyncMock()

    async def fetchrow(_sql, *args):
        if args[0] == "voice":
            return None
        if args[0] == "whatsapp":
            return {"business_id": "biz-wa"}
        return None

    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    business_id = await _resolve_business_id(fake, pool, Channel.VOICE, "PNID_1")
    assert business_id == "biz-wa"
    assert fake.store[channel_account_key("voice", "PNID_1")] == "biz-wa"


@pytest.mark.asyncio
async def test_whatsapp_does_not_fall_back_to_voice_row():
    """The voice mapping is one-way; inbound text must not resolve via voice."""
    from identity.resolver import _resolve_business_id

    fake = FakeCache()
    pool = AsyncMock()
    queried: list[str] = []

    async def fetchrow(_sql, *args):
        queried.append(args[0])
        return None

    pool.fetchrow = AsyncMock(side_effect=fetchrow)
    with pytest.raises(IdentityResolutionError):
        await _resolve_business_id(fake, pool, Channel.WHATSAPP, "PNID_1")
    assert queried == ["whatsapp"]


@pytest.mark.asyncio
async def test_facebook_comment_unknown_when_no_sibling_row():
    from identity.resolver import _resolve_business_id

    fake = FakeCache()
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(IdentityResolutionError) as ei:
        await _resolve_business_id(fake, pool, Channel.FACEBOOK_COMMENT, "PAGE_1")
    assert ei.value.code == IdentityErrorCode.UNKNOWN_CHANNEL_ACCOUNT
