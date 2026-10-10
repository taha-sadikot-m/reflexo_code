"""Stage T7: rich persona and Meta page auto-population."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_admin_persona_json_round_trips_and_keeps_rbac() -> None:
    from admin.routes.agent_config import PersonaUpdate, update_persona

    persona = {
        "identity": {"name": "Maya", "role": "Sales Assistant"},
        "boundaries": {"never_promise": ["same-day delivery"]},
    }
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "persona_name": "Maya",
            "persona_tone": "warm",
            "persona_language": ["en", "hi"],
            "custom_instructions": "",
            "greeting_message": "Hello",
            "persona": persona,
            "smart_number_mode": True,
            "smart_number_default": "ask_owner",
        }
    )
    with (
        patch(
            "admin.routes.agent_config.require_business",
            new=AsyncMock(return_value={"id": BUSINESS}),
        ),
        patch(
            "admin.routes.agent_config.get_admin_pool",
            new=AsyncMock(return_value=pool),
        ),
        patch(
            "admin.routes.agent_config.audit",
            new=AsyncMock(),
        ),
    ):
        result = await update_persona(
            BUSINESS,
            PersonaUpdate(
                persona_name="Maya",
                persona=persona,
                smart_number_mode=True,
                smart_number_default="ask_owner",
            ),
            SimpleNamespace(id="admin-id"),
        )
    assert result["persona"] == persona
    sql = pool.fetchrow.await_args.args[0]
    assert "persona =" in sql
    assert json.dumps(persona) in pool.fetchrow.await_args.args


def test_customer_prompt_includes_structured_persona_policy() -> None:
    from agent.prompts import build_customer_system_prompt
    from agent.specialist_prompts import build_inquiry_prompt

    config = {
        "name": "Store",
        "persona_name": "Maya",
        "persona": {
            "communication_style": {"formality": "casual_respectful"},
            "boundaries": {"never_say": ["guaranteed"]},
        },
    }
    for builder in (build_inquiry_prompt, build_customer_system_prompt):
        prompt = builder(config, None, "whatsapp", False, [], [])
        assert "[STRUCTURED PERSONA POLICY]" in prompt
        assert "casual_respectful" in prompt
        assert "never_say" in prompt


@pytest.mark.asyncio
async def test_meta_graph_account_data_is_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onboarding.integrations import get_instagram_accounts

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {
                "id": "page-1",
                "name": "Store Page",
                "category": "Shopping",
                "about": "Local store",
                "access_token": "page-token",
                "instagram_business_account": {
                    "id": "ig-1",
                    "username": "store",
                    "biography": "Great products",
                    "followers_count": 123,
                },
            }
        ]
    }
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    @asynccontextmanager
    async def client_context(*_args, **_kwargs):
        yield client

    monkeypatch.setattr("onboarding.integrations.httpx.AsyncClient", client_context)
    accounts = await get_instagram_accounts("token")
    assert accounts[0]["meta_page_data"]["page"]["category"] == "Shopping"
    assert (
        accounts[0]["meta_page_data"]["instagram"]["followers_count"]
        == 123
    )
    assert "followers_count" in client.get.await_args.kwargs["params"]["fields"]


@pytest.mark.asyncio
async def test_channel_store_persists_meta_page_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from onboarding.integrations import store_facebook_channels

    connection = MagicMock()
    connection.execute = AsyncMock()

    @asynccontextmanager
    async def connection_context(*_args, **_kwargs):
        yield connection

    monkeypatch.setenv("ENCRYPTION_KEY", "test-key")
    monkeypatch.setattr("onboarding.integrations.service_connection", connection_context)
    monkeypatch.setattr("onboarding.integrations.init_fernet", MagicMock())
    monkeypatch.setattr("onboarding.integrations.encrypt_token", lambda _token: b"encrypted")
    monkeypatch.setattr(
        "onboarding.integrations._invalidate_channel_caches",
        AsyncMock(),
    )
    metadata = {"id": "page-1", "category": "Shopping", "fan_count": 200}
    await store_facebook_channels(
        BUSINESS,
        [
            {
                "id": "page-1",
                "name": "Store",
                "access_token": "token",
                "meta_page_data": metadata,
            }
        ],
        "fallback",
    )
    sql = connection.execute.await_args.args[0]
    assert "meta_page_data" in sql
    assert json.dumps(metadata) in connection.execute.await_args.args
