"""Stage T6: Smart Number contact classification."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_duplicate_replies import (  # noqa: F401
    FakeRedis,
    as_customer,
    redis,
    session_env,
)

BUSINESS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CUSTOMER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.mark.asyncio
async def test_classify_contact_persists_and_personal_list_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_servers.reflexo_core.tools.customers import (
        classify_contact_impl,
        list_personal_contacts_impl,
    )

    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        return_value={"id": CUSTOMER, "contact_type": "personal"}
    )
    connection.fetch = AsyncMock(
        return_value=[
            {
                "id": CUSTOMER,
                "name": "Family",
                "contact_type": "personal",
            }
        ]
    )

    @asynccontextmanager
    async def connection_context(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(
        "mcp_servers.reflexo_core.tools.customers.tenant_connection",
        connection_context,
    )
    classified = await classify_contact_impl(
        BUSINESS,
        CUSTOMER,
        "personal",
    )
    contacts = await list_personal_contacts_impl(BUSINESS)
    assert classified["contact_type"] == "personal"
    assert contacts[0]["contact_type"] == "personal"
    assert "SET contact_type = $3" in connection.fetchrow.await_args.args[0]
    assert "contact_type = 'personal'" in connection.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_personal_contact_skips_all_temporal_business_automation() -> None:
    from workflow_builder.deployer import route_event_to_workflows

    with patch(
        "workflow_builder.deployer.matching_active",
        new=AsyncMock(),
    ) as matching:
        consumed = await route_event_to_workflows(
            {
                "type": "whatsapp_message",
                "contact_type": "personal",
                "smart_number_mode": True,
                "message": {"text": "hello"},
            },
            BUSINESS,
        )
    assert consumed is False
    matching.assert_not_awaited()


def test_smart_number_tools_are_bound_to_owner_config_specialist() -> None:
    from agent.registry import get_agent_definition
    from agent.tools import _OWNER_ONLY_CORE_TOOLS

    tools = get_agent_definition("config_agent").core_tools
    for name in (
        "classify_contact",
        "get_contact_type",
        "list_personal_contacts",
    ):
        assert name in tools
        assert name in _OWNER_ONLY_CORE_TOOLS


@pytest.mark.asyncio
async def test_smart_mode_personal_inbound_is_muted_before_workflow_and_agent(
    redis: FakeRedis,
    session_env: AsyncMock,
    as_customer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.session import handle_incoming_message

    as_customer()
    monkeypatch.setattr(
        "agent.session.load_business_config",
        AsyncMock(
            return_value={
                "id": BUSINESS,
                "onboarding_completed": True,
                "smart_number_mode": True,
            }
        ),
    )
    monkeypatch.setattr(
        "agent.session.load_customer_profile",
        AsyncMock(
            return_value={
                "id": CUSTOMER,
                "contact_type": "personal",
                "preferences": {},
                "tags": [],
            }
        ),
    )
    route = AsyncMock()
    monkeypatch.setattr("agent.pipeline.emit_ai_workflow_event", route)
    result = await handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919000000001",
        message_text="family message",
        receiving_account_id="pnid",
        external_message_id="wamid.personal",
    )
    assert result["status"] == "personal_contact"
    assert result["outbound_sent"] is False
    route.assert_not_awaited()
    session_env.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporal_activity_never_sends_to_personal_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow_builder.activities import execute_tool_activity

    monkeypatch.setattr(
        "workflow_builder.activities._is_personal_recipient",
        AsyncMock(return_value=True),
    )
    call = AsyncMock()
    monkeypatch.setattr("agent.tools.call_mcp_tool", call)
    result = await execute_tool_activity(
        "send_whatsapp_template",
        {"to": "+919000000001", "template_name": "offer"},
        BUSINESS,
    )
    assert result["status"] == "skipped_personal_contact"
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_mode_unknown_sender_still_creates_customer() -> None:
    """Smart Number adds classification but does not alter identity creation."""
    from identity.resolver import _resolve_customer

    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    created = AsyncMock(return_value=(CUSTOMER, "New customer", True))
    with patch("identity.resolver._create_new_customer", created):
        result = await _resolve_customer(
            cache,
            pool,
            BUSINESS,
            __import__("identity.models", fromlist=["Channel"]).Channel.WHATSAPP,
            "+919000000001",
            "New customer",
        )
    assert result == (CUSTOMER, "New customer", True)
    created.assert_awaited_once()
