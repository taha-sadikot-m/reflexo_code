"""Tests for resilient MCP tool catalog loading and send helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import tools as tools_mod
from agent.pipeline import (
    SendFailedError,
    claim_inbound_message,
    llm_already_sent_outbound,
    send_response,
)


def _fake_tool(name: str):
    return SimpleNamespace(name=name, description=name, args_schema=None)


@pytest.fixture(autouse=True)
def _reset_mcp_state():
    tools_mod._tools_by_name = None
    tools_mod._tool_server = {}
    tools_mod._available_servers = set()
    yield
    tools_mod._tools_by_name = None
    tools_mod._tool_server = {}
    tools_mod._available_servers = set()


@pytest.mark.asyncio
async def test_refresh_tool_catalog_partial_success(monkeypatch):
    async def fake_open(server_name: str):
        if server_name == "core":
            tools_mod._available_servers.add(server_name)
            return [_fake_tool("search_knowledge")]
        raise ConnectionError("meta down")

    monkeypatch.setattr(tools_mod, "_open_server_session", fake_open)

    catalog = await tools_mod.refresh_tool_catalog()
    assert "search_knowledge" in catalog
    assert tools_mod._tool_server["search_knowledge"] == "core"
    assert len(catalog) == 1


@pytest.mark.asyncio
async def test_refresh_tool_catalog_both_fail(monkeypatch):
    async def fake_open(server_name: str):
        raise ConnectionError(f"{server_name} down")

    monkeypatch.setattr(tools_mod, "_open_server_session", fake_open)

    with pytest.raises(RuntimeError, match="all MCP servers"):
        await tools_mod.refresh_tool_catalog()


@pytest.mark.asyncio
async def test_ensure_server_session_keeps_peer_tools(monkeypatch):
    """Opening Core must not clear an already-loaded Meta catalog."""
    tools_mod._tools_by_name = {
        "send_whatsapp_message": _fake_tool("send_whatsapp_message")
    }
    tools_mod._tool_server = {"send_whatsapp_message": "meta"}
    tools_mod._available_servers = {"meta"}

    opened: list[str] = []

    async def fake_open(server_name: str):
        opened.append(server_name)
        tools_mod._available_servers.add(server_name)
        return [_fake_tool("search_knowledge"), _fake_tool("log_message")]

    monkeypatch.setattr(tools_mod, "_open_server_session", fake_open)

    ok = await tools_mod.ensure_server_session("core")
    assert ok is True
    assert opened == ["core"]
    assert "send_whatsapp_message" in tools_mod._tools_by_name
    assert tools_mod._tool_server["send_whatsapp_message"] == "meta"
    assert "search_knowledge" in tools_mod._tools_by_name
    assert tools_mod._tool_server["search_knowledge"] == "core"
    assert "meta" in tools_mod._available_servers


@pytest.mark.asyncio
async def test_call_mcp_tool_uses_ensure_not_full_refresh(monkeypatch):
    ensure_calls: list[str] = []

    async def fake_ensure(server_name: str):
        ensure_calls.append(server_name)
        tools_mod._tools_by_name = tools_mod._tools_by_name or {}
        tools_mod._tools_by_name["log_message"] = _fake_tool("log_message")
        tools_mod._tool_server["log_message"] = "core"
        tools_mod._available_servers.add("core")
        return True

    async def fake_ainvoke(tool, server_name, kwargs):
        return {"ok": True, "tool": tool.name}

    monkeypatch.setattr(tools_mod, "ensure_server_session", fake_ensure)
    monkeypatch.setattr(tools_mod, "_ainvoke_with_session_lock", fake_ainvoke)
    tools_mod._tools_by_name = {
        "send_whatsapp_message": _fake_tool("send_whatsapp_message")
    }
    tools_mod._tool_server = {"send_whatsapp_message": "meta"}

    result = await tools_mod.call_mcp_tool(
        "core", "log_message", {"business_id": "b", "content": "hi"}
    )
    assert result["ok"] is True
    assert ensure_calls == ["core"]


@pytest.mark.asyncio
async def test_tool_call_recovers_after_mcp_restart(monkeypatch):
    class DeadTool:
        name = "send_instagram_dm"

        async def ainvoke(self, _kwargs):
            raise ConnectionError("streamablehttp disconnected")

    class ReconnectedTool:
        name = "send_instagram_dm"

        async def ainvoke(self, kwargs):
            return {"status": "sent", "to": kwargs["to_ig_user_id"]}

    async def reset(server_name):
        assert server_name == "meta"
        tools_mod._tools_by_name["send_instagram_dm"] = ReconnectedTool()
        tools_mod._tool_server["send_instagram_dm"] = "meta"

    tools_mod._tools_by_name = {"send_instagram_dm": DeadTool()}
    tools_mod._tool_server = {"send_instagram_dm": "meta"}
    monkeypatch.setattr(tools_mod, "reset_server_session", reset)

    result = await tools_mod._ainvoke_with_session_lock(
        DeadTool(),
        "meta",
        {"to_ig_user_id": "IG_USER"},
    )
    assert result == {"status": "sent", "to": "IG_USER"}


@pytest.mark.asyncio
async def test_close_mcp_sessions_clears_stateless_catalog():
    tools_mod._available_servers = {"core", "meta"}
    tools_mod._tools_by_name = {"x": _fake_tool("x")}
    await tools_mod.close_mcp_sessions()
    assert tools_mod._available_servers == set()
    assert tools_mod._tools_by_name is None


def test_format_exception_flattens_group():
    group = ExceptionGroup("tg", [ValueError("a"), RuntimeError("b")])
    text = tools_mod.format_exception(group)
    assert "ExceptionGroup" in text
    assert "ValueError: a" in text
    assert "RuntimeError: b" in text


def test_is_mcp_error_result():
    assert tools_mod.is_mcp_error_result({"error": {"code": "X", "message": "nope"}})
    assert not tools_mod.is_mcp_error_result({"status": "sent"})
    assert "X: nope" in tools_mod.mcp_error_message(
        {"error": {"code": "X", "message": "nope"}}
    )


def test_llm_already_sent_outbound():
    assert llm_already_sent_outbound(
        [{"tool_name": "send_whatsapp_message", "status": "sent", "message_id": "wamid.x"}]
    )
    assert llm_already_sent_outbound(
        [{"tool_name": "reply_instagram_comment", "message_id": "mid.1"}]
    )
    assert not llm_already_sent_outbound(
        [{"tool_name": "search_knowledge", "results": []}]
    )
    # Failed send must NOT count as already sent
    assert not llm_already_sent_outbound(
        [
            {
                "tool_name": "send_whatsapp_message",
                "error": {"code": "MESSAGE_FAILED", "message": "403"},
            }
        ]
    )
    # Name alone without success evidence is not enough
    assert not llm_already_sent_outbound([{"tool_name": "send_whatsapp_message"}])


def test_onboarding_extra_tools_listed():
    from onboarding.tools import (
        ONBOARDING_CONNECT_TOOLS,
        ONBOARDING_EXTRA_CORE_TOOLS,
        ONBOARDING_TOOL_DENYLIST,
    )

    assert "update_onboarding_progress" in ONBOARDING_EXTRA_CORE_TOOLS
    assert "complete_onboarding" in ONBOARDING_EXTRA_CORE_TOOLS
    assert "get_onboarding_progress" in ONBOARDING_EXTRA_CORE_TOOLS
    assert "connect_razorpay" in ONBOARDING_CONNECT_TOOLS
    assert "escalate_to_owner" in ONBOARDING_TOOL_DENYLIST
    assert "log_gap" in ONBOARDING_TOOL_DENYLIST


@pytest.mark.asyncio
async def test_send_response_raises_on_mcp_error(monkeypatch):
    async def fake_call(_server, _name, _args):
        return {"error": {"code": "MESSAGE_FAILED", "message": "HTTP 403 Forbidden"}}

    monkeypatch.setattr("agent.pipeline.call_mcp_tool", fake_call)
    with pytest.raises(SendFailedError, match="403"):
        await send_response(
            channel="whatsapp",
            recipient_id="+919999999999",
            text="Hello",
            business_id="00000000-0000-0000-0000-000000000001",
        )


@pytest.mark.asyncio
async def test_send_response_classifies_unapproved_meta_permission(monkeypatch):
    async def fake_call(_server, _name, _args):
        return {
            "error": {
                "code": "MESSAGE_FAILED",
                "message": "(#3) Application does not have the capability",
                "graph_code": 3,
                "http_status": 403,
                "retryable": False,
            }
        }

    async def fake_capabilities(_business_id):
        return {
            "items": [
                {
                    "features": [
                        {
                            "id": "instagram_dm",
                            "unapproved_app_permissions": [
                                "instagram_manage_messages"
                            ],
                        }
                    ]
                }
            ]
        }

    monkeypatch.setattr("agent.pipeline.call_mcp_tool", fake_call)
    monkeypatch.setattr(
        "integrations.capabilities.inspect_business_capabilities",
        fake_capabilities,
    )
    with pytest.raises(SendFailedError) as caught:
        await send_response(
            channel="instagram_dm",
            recipient_id="igsid.1",
            text="Hello",
            business_id="00000000-0000-0000-0000-000000000001",
        )
    assert caught.value.result["error"]["code"] == "APP_PERMISSION_UNAPPROVED"
    assert caught.value.result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_claim_inbound_message_dedup(monkeypatch):
    store: dict[str, str] = {}

    class FakeRedis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return False
            store[key] = value
            return True

        async def delete(self, key):
            store.pop(key, None)
            return 1

    monkeypatch.setattr("shared.redis_client.get_redis", lambda: FakeRedis())
    from agent.pipeline import release_inbound_message

    assert await claim_inbound_message("whatsapp", "wamid.1") is True
    assert await claim_inbound_message("whatsapp", "wamid.1") is False
    await release_inbound_message("whatsapp", "wamid.1")
    assert await claim_inbound_message("whatsapp", "wamid.1") is True
    assert await claim_inbound_message("whatsapp", None) is True
