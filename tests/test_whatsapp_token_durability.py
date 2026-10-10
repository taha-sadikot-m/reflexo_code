from __future__ import annotations

import pytest

from scripts import seed_whatsapp_channel


@pytest.mark.asyncio
async def test_whatsapp_seed_rejects_user_token_by_default(monkeypatch):
    async def inspect(_token):
        return {
            "is_valid": True,
            "type": "USER",
            "scopes": ["whatsapp_business_messaging"],
        }

    monkeypatch.setattr(seed_whatsapp_channel, "inspect_token", inspect)
    assert await seed_whatsapp_channel.preflight("not-printed", force=False) is False
    assert await seed_whatsapp_channel.preflight("not-printed", force=True) is True


@pytest.mark.asyncio
async def test_whatsapp_seed_accepts_system_user_token(monkeypatch):
    async def inspect(_token):
        return {
            "is_valid": True,
            "type": "SYSTEM_USER",
            "scopes": ["whatsapp_business_messaging"],
        }

    monkeypatch.setattr(seed_whatsapp_channel, "inspect_token", inspect)
    assert await seed_whatsapp_channel.preflight("not-printed", force=False) is True
