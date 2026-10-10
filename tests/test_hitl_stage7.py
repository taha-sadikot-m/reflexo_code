"""Stage 7 HITL gates + typing indicators."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from agent.confirmation import (
    HITL_APPROVE_ID,
    HITL_DECLINE_ID,
    maybe_confirm,
    parse_hitl_reply,
)
from agent.channel_ack import event_should_ack
from mcp_servers.meta_mcp.tools import common as common_tools


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class _DeleteArgs(BaseModel):
    business_id: str = ""
    entry_id: str = ""


# ── T7.3 / risk tiers ───────────────────────────────────────────────────────


def test_t73_readonly_tools_skip_confirmation():
    assert maybe_confirm("search_knowledge", {"query": "hours"}) is None
    assert maybe_confirm("list_workflow_rules", {}) is None
    assert maybe_confirm("get_business_config", {}) is None
    assert maybe_confirm("add_knowledge", {"question": "q"}) is None


def test_blocked_tier_does_not_interrupt():
    result = maybe_confirm("reset_knowledge_base", {})
    assert result is not None
    assert result["status"] == "blocked"


@pytest.mark.asyncio
async def test_t71_approve_runs_mcp(monkeypatch: pytest.MonkeyPatch):
    invoked: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "agent.confirmation.interrupt",
        lambda payload: {"approved": True},
    )

    async def _lock(_tool, _server, kwargs):
        invoked.append(kwargs)
        return {"status": "deleted", "entry_id": kwargs.get("entry_id")}

    monkeypatch.setattr("agent.tools._ainvoke_with_session_lock", _lock)

    async def _inner(**kwargs: Any) -> dict[str, Any]:
        """Delete a knowledge entry."""
        return {"status": "deleted"}

    base = StructuredTool.from_function(
        coroutine=_inner,
        name="delete_knowledge",
        description="Delete a knowledge entry",
        args_schema=_DeleteArgs,
    )
    from agent.tools import _wrap_with_business_id

    wrapped = _wrap_with_business_id(
        base, "biz-1", server_name="core", require_confirmation=True
    )
    out = await wrapped.ainvoke({"entry_id": "faq-9"})
    assert invoked and invoked[0]["entry_id"] == "faq-9"
    assert out.get("status") == "deleted"


@pytest.mark.asyncio
async def test_t72_decline_skips_mcp(monkeypatch: pytest.MonkeyPatch):
    invoked: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agent.confirmation.interrupt",
        lambda payload: {"approved": False},
    )

    async def _lock(_tool, _server, kwargs):
        invoked.append(kwargs)
        return {"status": "deleted"}

    monkeypatch.setattr("agent.tools._ainvoke_with_session_lock", _lock)

    async def _inner(**kwargs: Any) -> dict[str, Any]:
        """Delete a knowledge entry."""
        return {"status": "deleted"}

    base = StructuredTool.from_function(
        coroutine=_inner,
        name="delete_knowledge",
        description="Delete a knowledge entry",
        args_schema=_DeleteArgs,
    )
    from agent.tools import _wrap_with_business_id

    wrapped = _wrap_with_business_id(
        base, "biz-1", server_name="core", require_confirmation=True
    )
    out = await wrapped.ainvoke({"entry_id": "faq-9"})
    assert invoked == []
    assert out["status"] == "cancelled"


@pytest.mark.asyncio
async def test_customer_tools_not_gated(monkeypatch: pytest.MonkeyPatch):
    invoked: list[dict[str, Any]] = []

    def boom(_payload):
        raise AssertionError("interrupt must not run for customer wraps")

    monkeypatch.setattr("agent.confirmation.interrupt", boom)

    async def _lock(_tool, _server, kwargs):
        invoked.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("agent.tools._ainvoke_with_session_lock", _lock)

    async def _inner(**kwargs: Any) -> dict[str, Any]:
        """Ungated delete for customer wrap test."""
        return {"ok": True}

    base = StructuredTool.from_function(
        coroutine=_inner,
        name="delete_knowledge",
        description="Delete a knowledge entry",
        args_schema=_DeleteArgs,
    )
    from agent.tools import _wrap_with_business_id

    wrapped = _wrap_with_business_id(
        base, "biz-1", server_name="core", require_confirmation=False
    )
    await wrapped.ainvoke({"entry_id": "x"})
    assert invoked


def test_parse_hitl_reply_buttons_and_text():
    assert parse_hitl_reply("yes") is True
    assert parse_hitl_reply("NO") is False
    assert parse_hitl_reply("Confirm") is True
    assert parse_hitl_reply("Cancel") is False
    assert parse_hitl_reply("हाँ") is True
    assert parse_hitl_reply("delete something else") is None
    assert parse_hitl_reply("x", reply_id=HITL_APPROVE_ID) is True
    assert parse_hitl_reply("x", reply_id=HITL_DECLINE_ID) is False


@pytest.mark.asyncio
async def test_graph_interrupt_not_swallowed_by_tool_retry():
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Interrupt

    from agent.error_handling import ToolCallRetryWrapper

    async def _raise():
        raise GraphInterrupt((Interrupt(value={"tool": "delete_knowledge"}),))

    with pytest.raises(GraphInterrupt):
        await ToolCallRetryWrapper.execute_with_retry(_raise)


# ── T7.4 typing / mark_as_read ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t74_mark_as_read_includes_typing_indicator(monkeypatch: pytest.MonkeyPatch):
    sent: dict[str, Any] = {}

    async def business_exists(_bid: str) -> bool:
        return True

    async def check_rate_limit(*_a, **_k) -> None:
        return None

    async def ensure_valid_token(_bid: str, _ch: str) -> dict[str, Any]:
        return {"access_token": "tok", "phone_number_id": "pnid"}

    async def graph_post(path, token, body, **_k):
        sent["path"] = path
        sent["body"] = body
        return {}

    monkeypatch.setattr(common_tools, "business_exists", business_exists)
    monkeypatch.setattr(common_tools, "check_rate_limit", check_rate_limit)
    monkeypatch.setattr(common_tools, "ensure_valid_token", ensure_valid_token)
    monkeypatch.setattr(common_tools, "graph_post", graph_post)

    mcp = _FakeMcp()
    common_tools.register(mcp)
    result = await mcp.tools["mark_as_read"]("biz", "wamid.1", "whatsapp")
    assert result["status"] == "success"
    assert sent["body"]["status"] == "read"
    assert sent["body"]["typing_indicator"] == {"type": "text"}


@pytest.mark.asyncio
async def test_t74_ack_does_not_wait_for_the_handler(monkeypatch: pytest.MonkeyPatch):
    """The read receipt and typing indicator are dispatched off the pipeline.

    A turn can take a minute, so the ack runs as its own task. This blocks the
    handler until the ack lands: if the ack ever moved behind the pipeline again
    the wait_for below would time out.
    """
    order: list[str] = []
    acked = asyncio.Event()

    async def ack(**_k):
        order.append("ack")
        acked.set()

    async def handle(*_a, **_k):
        await acked.wait()
        order.append("handle")
        return {"status": "ok"}

    async def claim(*_a, **_k):
        return True

    monkeypatch.setattr("agent.channel_ack.acknowledge_inbound", ack)
    monkeypatch.setattr("agent.session.handle_incoming_message", handle)
    monkeypatch.setattr("agent.session.claim_inbound_message", claim)
    monkeypatch.setattr("agent.session.release_inbound_message", AsyncMock())
    monkeypatch.setattr("agent.pipeline.extract_message_text", lambda _c: "hello")
    monkeypatch.setattr(
        "agent.pipeline.receiving_account_from_event", lambda _e: "acct"
    )
    monkeypatch.setattr("agent.progress.publish_progress", AsyncMock())

    from agent.session import handle_redis_event

    await asyncio.wait_for(
        handle_redis_event(
            {
                "event_type": "message",
                "channel": "whatsapp",
                "sender_id": "+1555",
                "business_id": "biz",
                "message_id": "wamid.1",
                "phone_number_id": "acct",
                "content": {"text": "hello", "type": "text"},
            }
        ),
        timeout=5,
    )
    assert order == ["ack", "handle"]


def test_simulator_and_retry_skip_ack():
    assert event_should_ack({"simulator": {"mode": "owner"}}) is False
    assert event_should_ack({"skip_send": True}) is False
    assert event_should_ack({"event_type": "retry"}) is False
    assert event_should_ack({"event_type": "message"}) is True


# ── T7.5 broker status ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t75_broker_sends_one_status_message(monkeypatch: pytest.MonkeyPatch):
    sends: list[str] = []

    async def fake_send(**kwargs):
        sends.append(kwargs.get("text") or "")
        return {"status": "sent"}

    async def fake_specialist(*_a, **_k):
        return {
            "status": "ok",
            "response_text": "FAQ found",
            "tool_calls_made": ["search_knowledge"],
            "tool_results": [{"tool_name": "search_knowledge"}],
            "token_usage": None,
        }

    class FakeBroker:
        def __init__(self, tools):
            self._tools = {t.name: t for t in tools}

        async def ainvoke(self, _payload, config=None):
            tool = self._tools["delegate_to_agent"]
            text = await tool.ainvoke(
                {"agent_name": "analytics_agent", "task": "daily summary"}
            )
            from langchain_core.messages import AIMessage, HumanMessage

            return {
                "messages": [
                    HumanMessage(content="show analytics"),
                    AIMessage(content=str(text)),
                ]
            }

    monkeypatch.setattr("agent.broker.ChatGoogleGenerativeAI", lambda **_k: object())
    monkeypatch.setattr(
        "agent.broker.create_react_agent",
        lambda **kw: FakeBroker(kw["tools"]),
    )
    monkeypatch.setattr("agent.broker.run_specialist", fake_specialist)
    monkeypatch.setattr("agent.pipeline.send_response", fake_send)
    monkeypatch.setattr("agent.progress.publish_progress", AsyncMock())
    monkeypatch.setattr("agent.observability.build_langfuse_callbacks", lambda **_k: None)
    monkeypatch.setattr("agent.broker.get_agent_settings", lambda: type("S", (), {"google_api_key": "x"})())

    from agent.broker import BROKER_STATUS_MESSAGE, run_broker_path

    out = await run_broker_path(
        "show analytics",
        business_config={"id": "biz-1", "name": "Cafe"},
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
        checkpointer=object(),
        store=object(),
        thread_id="biz:owner:whatsapp",
        recipient_id="+1555",
        skip_send=False,
        progress_session_id="sess",
    )
    assert sends.count(BROKER_STATUS_MESSAGE) == 1
    assert out.get("status") != "confirmation_required"


@pytest.mark.asyncio
async def test_broker_hitl_payload_passthrough(monkeypatch: pytest.MonkeyPatch):
    async def fake_specialist(*_a, **_k):
        return {
            "status": "confirmation_required",
            "hitl": {
                "tool": "delete_knowledge",
                "preview": "delete_knowledge (entry_id=faq-1)",
                "specialist": "knowledge_agent",
                "thread_id": "t:spec:knowledge_agent",
            },
            "response_text": "",
            "tool_calls_made": ["delete_knowledge"],
            "tool_results": [],
        }

    class FakeBroker:
        def __init__(self, tools):
            self._tools = {t.name: t for t in tools}

        async def ainvoke(self, _payload, config=None):
            tool = self._tools["delegate_to_agent"]
            await tool.ainvoke(
                {"agent_name": "knowledge_agent", "task": "Delete this FAQ entry"}
            )
            from langchain_core.messages import AIMessage, HumanMessage

            return {"messages": [HumanMessage(content="x"), AIMessage(content="ok")]}

    monkeypatch.setattr("agent.broker.ChatGoogleGenerativeAI", lambda **_k: object())
    monkeypatch.setattr(
        "agent.broker.create_react_agent",
        lambda **kw: FakeBroker(kw["tools"]),
    )
    monkeypatch.setattr("agent.broker.run_specialist", fake_specialist)
    monkeypatch.setattr("agent.pipeline.send_response", AsyncMock())
    monkeypatch.setattr("agent.progress.publish_progress", AsyncMock())
    monkeypatch.setattr("agent.observability.build_langfuse_callbacks", lambda **_k: None)
    monkeypatch.setattr("agent.broker.get_agent_settings", lambda: type("S", (), {"google_api_key": "x"})())

    from agent.broker import run_broker_path

    out = await run_broker_path(
        "Delete this FAQ entry",
        business_config={"id": "biz-1", "name": "Cafe"},
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
        checkpointer=object(),
        store=object(),
        thread_id="biz:owner:whatsapp",
        recipient_id="+1555",
        skip_send=True,
    )
    assert out["status"] == "confirmation_required"
    assert out["hitl"]["tool"] == "delete_knowledge"


def test_simulator_requires_key(monkeypatch: pytest.MonkeyPatch):
    from fastapi import HTTPException

    from api.settings import get_api_settings
    from api.simulator import _require_simulator_key

    monkeypatch.setenv("SIMULATOR_API_KEY", "secret-key")
    get_api_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        _require_simulator_key(None)
    assert exc.value.status_code == 401
    _require_simulator_key("secret-key")
    get_api_settings.cache_clear()
