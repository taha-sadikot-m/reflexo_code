"""Stage 11 — per-business custom MCP connector."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool

from admin.auth import AdminUser, get_current_admin
from agent.broker_prompts import broker_delegate_names, build_broker_system_prompt
from agent.mcp_connector import (
    DISCOVER_TIMEOUT_SECONDS,
    _tool_cache,
    get_business_mcp_tools,
    has_mcp_connections,
    invalidate_tool_cache,
)
from agent.registry import (
    CUSTOM_TOOLS_AGENT,
    get_agent_definition,
    planned_tool_names,
)
from agent.specialist_prompts import PROMPT_BUILDERS, build_specialist_prompt

BIZ_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BIZ_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _clear_mcp_cache():
    _tool_cache.clear()
    yield
    _tool_cache.clear()


def _tool(name: str, result: str = "ok") -> StructuredTool:
    async def _run(**_kwargs: Any) -> str:
        return result

    return StructuredTool.from_function(coroutine=_run, name=name, description=name)


def test_custom_tools_agent_is_owner_only() -> None:
    defn = get_agent_definition(CUSTOM_TOOLS_AGENT)
    assert defn.session_type == "owner"
    names = planned_tool_names(CUSTOM_TOOLS_AGENT, "whatsapp")
    assert "search_knowledge" in names
    assert "get_customer" in names
    assert "handoff_to_agent" in names
    assert "send_whatsapp_message" not in names


def test_custom_tools_prompt_forbids_invented_crm_facts() -> None:
    assert "build_custom_tools_prompt" in PROMPT_BUILDERS
    prompt = build_specialist_prompt(
        "build_custom_tools_prompt",
        business_config={"name": "HolonCode", "owner_name": "Taha"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "prefixed" in prompt.lower() or "connection__tool" in prompt
    assert "Never invent" in prompt


def test_broker_prompt_hides_custom_tools_without_connections() -> None:
    prompt = build_broker_system_prompt({"name": "HolonCode", "id": BIZ_A})
    assert CUSTOM_TOOLS_AGENT not in prompt
    assert CUSTOM_TOOLS_AGENT not in broker_delegate_names(has_custom_mcp=False)


def test_broker_prompt_includes_custom_tools_when_connected() -> None:
    prompt = build_broker_system_prompt(
        {"name": "HolonCode", "id": BIZ_A},
        has_custom_mcp=True,
    )
    assert CUSTOM_TOOLS_AGENT in prompt
    assert CUSTOM_TOOLS_AGENT in broker_delegate_names(has_custom_mcp=True)
    assert "custom MCP" in prompt


@pytest.mark.asyncio
async def test_discover_prefixes_tool_names_and_caches() -> None:
    raw = [_tool("lookup_customer", "Rahul")]
    client = MagicMock()
    client.get_tools = AsyncMock(return_value=raw)

    with (
        patch(
            "agent.mcp_connector.list_connections_internal",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "c1",
                        "name": "crm",
                        "server_url": "http://127.0.0.1:8099/mcp",
                        "auth_config": {},
                    }
                ]
            ),
        ),
        patch("agent.mcp_connector.record_health", new=AsyncMock()),
        patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            return_value=client,
        ),
    ):
        first = await get_business_mcp_tools(BIZ_A)
        second = await get_business_mcp_tools(BIZ_A)

    assert [t.name for t in first] == ["crm__lookup_customer"]
    assert second == first
    client.get_tools.assert_awaited_once()
    result = await first[0].ainvoke({})
    assert result == "Rahul"


@pytest.mark.asyncio
async def test_invalidate_tool_cache_refetches() -> None:
    client = MagicMock()
    client.get_tools = AsyncMock(side_effect=[[_tool("a")], [_tool("b")]])
    with (
        patch(
            "agent.mcp_connector.list_connections_internal",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "c1",
                        "name": "crm",
                        "server_url": "http://127.0.0.1:8099/mcp",
                        "auth_config": {},
                    }
                ]
            ),
        ),
        patch("agent.mcp_connector.record_health", new=AsyncMock()),
        patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            return_value=client,
        ),
    ):
        first = await get_business_mcp_tools(BIZ_A)
        invalidate_tool_cache(BIZ_A)
        second = await get_business_mcp_tools(BIZ_A)
    assert first[0].name == "crm__a"
    assert second[0].name == "crm__b"
    assert client.get_tools.await_count == 2


@pytest.mark.asyncio
async def test_down_server_times_out_without_raising_to_the_agent() -> None:
    async def _hang():
        await asyncio.sleep(DISCOVER_TIMEOUT_SECONDS + 2)
        return []

    client = MagicMock()
    client.get_tools = _hang

    with (
        patch(
            "agent.mcp_connector.list_connections_internal",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "c1",
                        "name": "crm",
                        "server_url": "http://127.0.0.1:1/mcp",
                        "auth_config": {},
                    }
                ]
            ),
        ),
        patch("agent.mcp_connector.record_health", new=AsyncMock()) as health,
        patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            return_value=client,
        ),
        patch("agent.mcp_connector.DISCOVER_TIMEOUT_SECONDS", 0.05),
    ):
        tools = await get_business_mcp_tools(BIZ_A)

    assert tools == []
    health.assert_awaited()
    assert health.await_args.kwargs["status"] == "error"


@pytest.mark.asyncio
async def test_two_businesses_do_not_share_tools() -> None:
    async def fake_list(business_id: str, *, active_only: bool = True):
        if business_id == BIZ_A:
            return [
                {
                    "id": "a1",
                    "name": "crm_a",
                    "server_url": "http://a/mcp",
                    "auth_config": {},
                }
            ]
        return [
            {
                "id": "b1",
                "name": "crm_b",
                "server_url": "http://b/mcp",
                "auth_config": {},
            }
        ]

    def fake_client(config):
        name = next(iter(config))
        client = MagicMock()
        client.get_tools = AsyncMock(return_value=[_tool(f"lookup_{name}")])
        return client

    with (
        patch("agent.mcp_connector.list_connections_internal", new=fake_list),
        patch("agent.mcp_connector.record_health", new=AsyncMock()),
        patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            side_effect=fake_client,
        ),
        patch(
            "agent.mcp_security.validate_remote_url",
            new=AsyncMock(side_effect=lambda url: url),
        ),
    ):
        tools_a = await get_business_mcp_tools(BIZ_A)
        tools_b = await get_business_mcp_tools(BIZ_B)

    assert [t.name for t in tools_a] == ["crm_a__lookup_crm_a"]
    assert [t.name for t in tools_b] == ["crm_b__lookup_crm_b"]


@pytest.mark.asyncio
async def test_expired_cache_is_refetched() -> None:
    client = MagicMock()
    client.get_tools = AsyncMock(side_effect=[[_tool("one")], [_tool("two")]])
    with (
        patch(
            "agent.mcp_connector.list_connections_internal",
            new=AsyncMock(
                return_value=[
                    {
                        "id": "c1",
                        "name": "crm",
                        "server_url": "http://127.0.0.1:8099/mcp",
                        "auth_config": {},
                    }
                ]
            ),
        ),
        patch("agent.mcp_connector.record_health", new=AsyncMock()),
        patch(
            "langchain_mcp_adapters.client.MultiServerMCPClient",
            return_value=client,
        ),
    ):
        await get_business_mcp_tools(BIZ_A)
        _tool_cache[BIZ_A]["fetched_at"] = datetime.now(timezone.utc) - timedelta(
            seconds=10_000
        )
        second = await get_business_mcp_tools(BIZ_A)
    assert second[0].name == "crm__two"


@pytest.mark.asyncio
async def test_factory_injects_prefixed_tools_only_for_custom_agent() -> None:
    from agent.factory import create_specialist_agent

    captured: dict[str, Any] = {}

    def fake_react(*, tools, **_kwargs):
        captured["names"] = [t.name for t in tools]
        return SimpleNamespace(tools=tools)

    custom = [_tool("crm__lookup_customer")]
    with (
        patch("agent.factory.ChatGoogleGenerativeAI", return_value=MagicMock()),
        patch("agent.factory.create_react_agent", side_effect=fake_react),
        patch("agent.tools.ensure_all_mcp_sessions", new=AsyncMock()),
        patch("agent.factory.tools_for_names", new=AsyncMock(side_effect=lambda *_a, **_k: [_tool("search_knowledge")])),
        patch("agent.handoff.build_handoff_tool", return_value=_tool("handoff_to_agent")),
        patch(
            "agent.mcp_connector.get_business_mcp_tools",
            new=AsyncMock(return_value=custom),
        ) as discover,
        patch("agent.factory.build_specialist_prompt", return_value="prompt"),
    ):
        await create_specialist_agent(
            "custom_tools_agent",
            {"id": BIZ_A, "name": "HolonCode"},
            None,
            "whatsapp",
            False,
            [],
            [],
            MagicMock(),
            MagicMock(),
        )
        discover.assert_awaited_once_with(BIZ_A)
        assert "crm__lookup_customer" in captured["names"]

        captured.clear()
        discover.reset_mock()
        await create_specialist_agent(
            "inquiry_agent",
            {"id": BIZ_A, "name": "HolonCode"},
            None,
            "whatsapp",
            False,
            [],
            [],
            MagicMock(),
            MagicMock(),
        )
        discover.assert_not_awaited()
        assert "crm__lookup_customer" not in captured.get("names", [])


def test_store_name_and_url_validation() -> None:
    from agent.mcp_store import (
        McpStoreError,
        decrypt_auth_headers,
        validate_connection_name,
        validate_server_url,
    )

    assert validate_connection_name("My-CRM") == "my_crm"
    with pytest.raises(McpStoreError):
        validate_connection_name("1bad")
    with pytest.raises(McpStoreError):
        validate_server_url("ftp://x")
    assert validate_server_url("http://127.0.0.1:8099/mcp/") == "http://127.0.0.1:8099/mcp"
    assert decrypt_auth_headers("{}") == {}
    assert decrypt_auth_headers({"headers": {"Authorization": "Bearer x"}}) == {}


def _admin_app() -> TestClient:
    from admin.routes.mcp_connections import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: AdminUser(
        id="11111111-1111-1111-1111-111111111111",
        email="admin@reflexo.ai",
        name="Admin",
        role="admin",
        is_active=True,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_admin_crud_and_test_endpoint() -> None:
    stored: dict[str, Any] = {}

    async def fake_insert(business_id, **kwargs):
        stored.update({"id": "conn-1", "business_id": business_id, **kwargs, "is_active": True, "discovered_tools": []})
        return dict(stored)

    async def fake_list(business_id, *, active_only=False):
        del business_id, active_only
        return [dict(stored)] if stored else []

    async def fake_update(business_id, connection_id, **kwargs):
        del business_id
        stored["id"] = connection_id
        if kwargs.get("is_active") is not None:
            stored["is_active"] = kwargs["is_active"]
        return dict(stored)

    async def fake_delete(business_id, connection_id):
        del business_id, connection_id
        stored.clear()
        return True

    with (
        patch("admin.routes.mcp_connections.require_business", new=AsyncMock(return_value={"id": BIZ_A})),
        patch("admin.routes.mcp_connections.audit", new=AsyncMock()),
        patch("admin.routes.mcp_connections.insert_connection", new=fake_insert),
        patch("admin.routes.mcp_connections.list_connections", new=fake_list),
        patch("admin.routes.mcp_connections.update_connection", new=fake_update),
        patch("admin.routes.mcp_connections.delete_connection", new=fake_delete),
        patch(
            "admin.routes.mcp_connections.get_connection",
            new=AsyncMock(return_value={"id": "conn-1", "name": "crm"}),
        ),
        patch(
            "agent.mcp_store.get_connection_internal",
            new=AsyncMock(
                return_value={
                    "id": "conn-1",
                    "name": "crm",
                    "server_url": "http://127.0.0.1:8099/mcp",
                    "auth_config": {},
                }
            ),
        ),
        patch("admin.routes.mcp_connections.record_health", new=AsyncMock()),
        patch(
            "agent.mcp_connector.probe_mcp_server",
            new=AsyncMock(
                return_value={"ok": True, "status": "healthy", "tools": ["crm__lookup_customer"]}
            ),
        ),
        patch("agent.mcp_connector.invalidate_tool_cache"),
    ):
        client = _admin_app()
        created = client.post(
            f"/api/admin/mcp-connections/{BIZ_A}",
            json={"name": "crm", "server_url": "http://127.0.0.1:8099/mcp"},
        )
        assert created.status_code == 200, created.text
        listed = client.get(f"/api/admin/mcp-connections/{BIZ_A}")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["name"] == "crm"
        probed = client.post(f"/api/admin/mcp-connections/{BIZ_A}/conn-1/test")
        assert probed.status_code == 200
        assert probed.json()["ok"] is True
        deleted = client.delete(f"/api/admin/mcp-connections/{BIZ_A}/conn-1")
        assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_has_mcp_connections_reads_active_count() -> None:
    with patch("agent.mcp_connector.count_active", new=AsyncMock(return_value=2)):
        assert await has_mcp_connections(BIZ_A) is True
    with patch("agent.mcp_connector.count_active", new=AsyncMock(return_value=0)):
        assert await has_mcp_connections(BIZ_A) is False
