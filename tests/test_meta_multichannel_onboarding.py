"""UX + wiring tests for Meta multi-channel onboarding (WA + IG + FB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.prompts import build_owner_system_prompt
from agent.tools import OWNER_CONNECT_TOOLS, _OWNER_ONLY_CORE_TOOLS
from identity.models import Channel
from identity.resolver import _check_is_owner
from onboarding.prompts import build_onboarding_system_prompt
from onboarding.progress import OnboardingProgress
from onboarding.tools import ONBOARDING_EXTRA_CORE_TOOLS
from tests.test_identity_resolution import FakeCache


def test_owner_allowlist_includes_meta_oauth_tools() -> None:
    for name in ("generate_instagram_oauth_url", "generate_facebook_oauth_url"):
        assert name in OWNER_CONNECT_TOOLS
        assert name in _OWNER_ONLY_CORE_TOOLS
        assert name in ONBOARDING_EXTRA_CORE_TOOLS


def test_onboarding_prompt_names_meta_oauth_and_channel() -> None:
    progress = OnboardingProgress(
        business_id="biz-1",
        owner_name="Taha",
        business_name="HolonCode",
        what_they_sell="IT services",
    )
    prompt = build_onboarding_system_prompt(
        {"id": "biz-1", "name": "HolonCode", "owner_name": "Taha"},
        progress,
        channel="instagram_dm",
        is_voice=False,
        cross_channel_messages=[
            {
                "role": "owner",
                "content": "Set hours Mon-Fri 9-5",
                "channel": "whatsapp",
            }
        ],
    )
    assert "via Instagram" in prompt
    assert "generate_instagram_oauth_url" in prompt
    assert "generate_facebook_oauth_url" in prompt
    assert "[CONTEXT FROM OTHER CHANNELS]" in prompt
    assert "Set hours Mon-Fri 9-5" in prompt
    assert "Never say Meta is unavailable" in prompt


def test_owner_prompt_names_meta_oauth_and_cross_channel() -> None:
    prompt = build_owner_system_prompt(
        {"name": "HolonCode", "owner_name": "Taha", "persona_name": "Maya"},
        channel="facebook_messenger",
        is_voice=False,
        cross_channel_messages=[
            {"role": "assistant", "content": "WhatsApp setup done", "channel": "whatsapp"}
        ],
    )
    assert "generate_instagram_oauth_url" in prompt
    assert "generate_facebook_oauth_url" in prompt
    assert "[CONTEXT FROM OTHER CHANNELS]" in prompt
    assert "WhatsApp setup done" in prompt


def test_progress_summary_surfaces_facebook_connected() -> None:
    p = OnboardingProgress(
        business_id="b1",
        business_name="Cool Style",
        instagram_connected=True,
        facebook_connected=True,
    )
    summary = p.generate_progress_summary()
    assert "Instagram: connected" in summary
    assert "Facebook Messenger: connected" in summary
    # Recommended remaining stays IG-aligned (FB via prompt ask, not checklist)
    assert "facebook" not in p.get_recommended_remaining()


@pytest.mark.asyncio
async def test_load_owner_cross_channel_context_excludes_current() -> None:
    from agent.memory import load_owner_cross_channel_context

    wa_row = {
        "role": "owner",
        "content": "Connect Gmail next",
        "channel": "whatsapp",
        "created_at": None,
    }
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[wa_row])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("agent.memory.tenant_connection", return_value=cm):
        msgs = await load_owner_cross_channel_context("biz-1", "instagram_dm", limit=5)

    assert len(msgs) == 1
    assert msgs[0]["channel"] == "whatsapp"
    assert msgs[0]["content"] == "Connect Gmail next"
    sql = conn.fetch.await_args.args[0]
    assert "customer_id IS NULL" in sql
    assert conn.fetch.await_args.args[1] == "biz-1"
    assert conn.fetch.await_args.args[2] == "instagram_dm"


@pytest.mark.asyncio
async def test_link_owner_channel_identity_makes_check_is_owner_true() -> None:
    from identity.owner_link import link_owner_channel_identity

    biz_id = "00000000-0000-0000-0000-000000000001"
    igsid = "IGS_OWNER_TEST_123"
    fake = FakeCache()

    pool = AsyncMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "id": biz_id,
            "owner_phone": "+919999999999",
            "owner_email": "owner@example.com",
            "owner_name": "Taha",
        }
    )

    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(side_effect=["cust-owner", None])
    # first fetchval: find customer; we simulate miss then create — simplify: found
    conn.fetchval = AsyncMock(return_value="cust-owner")
    conn.execute = AsyncMock()

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)

    with (
        patch("identity.owner_link.get_pool", return_value=pool),
        patch("identity.owner_link.IdentityCache", return_value=fake),
    ):
        result = await link_owner_channel_identity(
            biz_id, "instagram_dm", igsid, display_name="Taha"
        )

    assert result["status"] == "linked"
    assert result["customer_id"] == "cust-owner"
    assert result["external_id"] == igsid

    # _check_is_owner should hit DB match after cache cleared
    async def fetchval_owner(sql, *args):
        if "identity_map" in sql:
            return 1
        return None

    pool2 = AsyncMock()
    pool2.fetchval = AsyncMock(side_effect=fetchval_owner)

    with patch("identity.resolver.IdentityCache", return_value=fake):
        is_owner = await _check_is_owner(
            fake, pool2, biz_id, Channel.INSTAGRAM_DM, igsid
        )
    assert is_owner is True
