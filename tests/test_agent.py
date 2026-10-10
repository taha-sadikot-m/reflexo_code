"""Unit tests for agent prompts, confidence, and Redis event mapping."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from agent.confidence import (
    calculate_confidence,
    has_authoritative_evidence,
    should_force_low_confidence_escalation,
)
from agent.config import IGNORED_EVENT_TYPES, MESSAGE_EVENT_TYPES
from agent.pipeline import extract_message_text, receiving_account_from_event
from agent.prompts import (
    build_customer_system_prompt,
    build_owner_system_prompt,
    _format_business_hours,
)


def test_format_business_hours_closed_and_open():
    text = _format_business_hours(
        {
            "mon": {"open": "10:00", "close": "18:00"},
            "sun": {"closed": True},
        }
    )
    assert "Mon 10:00 - 18:00" in text
    assert "Sun Closed" in text


def test_customer_prompt_includes_persona_and_delivery_rule():
    prompt = build_customer_system_prompt(
        business_config={
            "name": "Cool Store",
            "persona_name": "Maya",
            "persona_tone": "friendly",
            "persona_language": ["en", "hi"],
            "vertical": "ecommerce",
            "business_hours": {"mon": {"open": "10:00", "close": "19:00"}},
            "address": {"city": "Mumbai"},
            "custom_instructions": "Mention free delivery",
            "greeting_message": "Hi!",
        },
        customer_profile={"name": "Priya", "phone": "+919876543210"},
        channel="whatsapp",
        is_voice=False,
        recent_messages=[{"role": "customer", "content": "Hello"}],
        cross_channel_messages=[],
    )
    assert "Maya" in prompt
    assert "Cool Store" in prompt
    assert "Do NOT call send_*" in prompt
    assert "[Customer] Hello" in prompt
    assert "WHATSAPP RULES" in prompt
    assert "I currently support English and Hindi" in prompt
    assert "[LONG-TERM CUSTOMER MEMORY]" in prompt


def test_customer_prompt_includes_stored_business_facts():
    prompt = build_customer_system_prompt(
        business_config={
            "name": "HolonCode",
            "persona_name": "Maya",
            "persona_language": ["en"],
            "business_context": {"contact": {"email": "taha.sadikot.m@gmail.com"}},
        },
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "[STORED BUSINESS FACTS]" in prompt
    assert "taha.sadikot.m@gmail.com" in prompt


def test_customer_prompt_routes_generic_services_to_catalog_listing():
    prompt = build_customer_system_prompt(
        business_config={"name": "HolonCode", "persona_name": "Maya"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "FIRST call list_entities" in prompt
    assert 'entity_type "product", "service", or "offering"' in prompt
    assert "Never log a gap while a matching entity" in prompt


def test_owner_prompt_mentions_upsert_context():
    prompt = build_owner_system_prompt(
        business_config={
            "name": "Cool Store",
            "persona_name": "Maya",
            "owner_name": "Ravi",
            "business_context": {"extra": {"parking": "basement"}},
        },
        channel="whatsapp",
        is_voice=False,
    )
    assert "upsert_business_context" in prompt
    assert "basement" in prompt


def test_customer_prompt_includes_memory():
    prompt = build_customer_system_prompt(
        business_config={"name": "Biz", "persona_name": "A", "persona_language": ["en"]},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
        customer_memory={
            "preferences": {"size": "M"},
            "interaction_summary": "Asked about jackets",
        },
    )
    assert "size=M" in prompt
    assert "Asked about jackets" in prompt


def test_owner_prompt_management_mode():
    prompt = build_owner_system_prompt(
        business_config={"name": "Cool Store", "persona_name": "Maya", "owner_name": "Ravi"},
        channel="whatsapp",
        is_voice=False,
    )
    assert "OWNER SESSION" in prompt
    assert "Ravi" in prompt


def test_voice_rules_in_prompt():
    prompt = build_customer_system_prompt(
        business_config={"name": "Biz", "persona_name": "A"},
        customer_profile=None,
        channel="voice",
        is_voice=True,
        recent_messages=[],
        cross_channel_messages=[],
    )
    assert "VOICE MODE RULES" in prompt


def test_confidence_high_with_strong_signals():
    assessment = calculate_confidence(
        tool_calls_made=["search_knowledge", "search_entities", "get_customer", "get_business_hours"],
        tool_results=[
            {"tool_name": "search_knowledge", "results": [{"confidence": 0.9}]},
            {"tool_name": "search_entities", "total_results": 2},
        ],
        search_results=[{"confidence": 0.9}],
        gap_logged=False,
        escalation_triggered=False,
    )
    # +0.40 KB +0.20 entities +0.15 hours +0.10 customer +0.15 success >= 1.0
    assert assessment.tier == "HIGH"
    assert assessment.score > 0.8


def test_confidence_medium_on_kb_only():
    assessment = calculate_confidence(
        tool_calls_made=["search_knowledge"],
        tool_results=[{"tool_name": "search_knowledge", "results": [{"confidence": 0.9}]}],
        search_results=[{"confidence": 0.9}],
        gap_logged=False,
        escalation_triggered=False,
    )
    assert assessment.tier == "MEDIUM"


def test_confidence_hours_only_not_low():
    assessment = calculate_confidence(
        tool_calls_made=["get_business_hours", "get_business_hours"],
        tool_results=[
            {"tool_name": "get_business_hours", "hours": {}},
            {"tool_name": "get_business_hours", "hours": {}},
        ],
        search_results=None,
        gap_logged=False,
        escalation_triggered=False,
    )
    assert assessment.tier != "LOW"
    assert assessment.tier == "MEDIUM"
    assert "Business context data was available" in assessment.reasons
    assert assessment.should_escalate is False


def test_confidence_entity_catalog_only_is_grounded():
    tool_results = [
        {
            "tool_name": "list_entities",
            "entities": [
                {"id": "service-1", "entity_type": "service", "name": "Web Development"}
            ],
            "total_count": 1,
        }
    ]
    assessment = calculate_confidence(
        tool_calls_made=["list_entities"],
        tool_results=tool_results,
        search_results=None,
        gap_logged=False,
        escalation_triggered=False,
    )
    assert assessment.tier == "MEDIUM"
    assert assessment.score == pytest.approx(0.55)
    assert assessment.should_escalate is False
    assert has_authoritative_evidence(tool_results, None) is True


def test_confidence_kb_miss_does_not_override_entity_hit():
    tool_results = [
        {"tool_name": "search_knowledge", "results": []},
        {
            "tool_name": "search_entities",
            "results": [{"id": "service-1", "name": "AI Solutions"}],
            "total_results": 1,
        },
    ]
    assessment = calculate_confidence(
        tool_calls_made=["search_knowledge", "search_entities"],
        tool_results=tool_results,
        search_results=None,
        gap_logged=False,
        escalation_triggered=False,
    )
    assert assessment.tier == "MEDIUM"
    assert "KB search returned no results" not in assessment.reasons
    assert assessment.should_escalate is False


def test_empty_entity_catalog_is_not_authoritative_evidence():
    tool_results = [
        {
            "tool_name": "list_entities",
            "entities": [],
            "total_count": 0,
        }
    ]
    assessment = calculate_confidence(
        tool_calls_made=["list_entities"],
        tool_results=tool_results,
        search_results=None,
        gap_logged=False,
        escalation_triggered=False,
    )
    assert assessment.tier == "LOW"
    assert has_authoritative_evidence(tool_results, None) is False


def test_get_entity_counts_as_authoritative_evidence():
    tool_results = [
        {
            "tool_name": "get_entity",
            "id": "service-1",
            "entity_type": "service",
            "name": "MVP Development",
        }
    ]
    assert has_authoritative_evidence(tool_results, None) is True


def test_catalog_evidence_blocks_automatic_gap_escalation():
    tool_results = [
        {
            "tool_name": "list_entities",
            "entities": [{"id": "service-1", "name": "Web Development"}],
            "total_count": 1,
        },
        {"tool_name": "log_gap", "id": "gap-1"},
    ]
    assessment = calculate_confidence(
        tool_calls_made=["list_entities", "log_gap"],
        tool_results=tool_results,
        search_results=None,
        gap_logged=True,
        escalation_triggered=False,
    )
    assert assessment.tier == "LOW"
    assert (
        should_force_low_confidence_escalation(
            assessment,
            ["list_entities", "log_gap"],
            tool_results,
            None,
        )
        is False
    )


def test_confidence_low_on_gap_and_escalation():
    assessment = calculate_confidence(
        tool_calls_made=["search_knowledge", "log_gap", "escalate_to_owner"],
        tool_results=[{"tool_name": "search_knowledge", "error": "x"}],
        search_results=None,
        gap_logged=True,
        escalation_triggered=True,
    )
    assert assessment.tier == "LOW"
    assert assessment.should_escalate is True


def test_confidence_direct_greeting():
    assessment = calculate_confidence(
        tool_calls_made=[],
        tool_results=[],
        search_results=None,
        gap_logged=False,
        escalation_triggered=False,
    )
    assert assessment.tier == "HIGH"


def test_extract_message_text_variants():
    assert extract_message_text("hi") == "hi"
    assert extract_message_text({"text": "hello"}) == "hello"
    assert extract_message_text({"body": {"text": "nested"}}) == "nested"
    assert "a" in extract_message_text({"foo": "a"})


def test_receiving_account_from_event():
    assert receiving_account_from_event({"phone_number_id": "pn1"}) == "pn1"
    assert receiving_account_from_event({"ig_account_id": "ig1"}) == "ig1"
    assert receiving_account_from_event({"page_id": "pg1"}) == "pg1"


def test_message_event_type_sets():
    assert "whatsapp_message" in MESSAGE_EVENT_TYPES
    assert "whatsapp_status" in IGNORED_EVENT_TYPES


@pytest.mark.asyncio
async def test_handle_redis_event_ignores_status(monkeypatch):
    from agent import session as session_mod

    called = {"n": 0}

    async def _fake(*_a, **_k):
        called["n"] += 1
        return {"status": "responded"}

    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    result = await session_mod.handle_redis_event(
        {"event_type": "whatsapp_status", "channel": "whatsapp", "sender_id": "+91"}
    )
    assert result is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_handle_redis_event_maps_whatsapp(monkeypatch):
    from agent import session as session_mod

    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return {"status": "responded", "response_text": "ok"}

    async def _claim(*_a, **_k):
        return True

    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    monkeypatch.setattr(session_mod, "claim_inbound_message", _claim)
    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.1",
            "message_type": "text",
            "content": {"text": "What are your hours?"},
        }
    )
    assert result["status"] == "responded"
    assert captured["message_text"] == "What are your hours?"
    assert captured["receiving_account_id"] == "PNID"
    assert captured["external_sender_id"] == "+919999999999"


@pytest.mark.asyncio
async def test_handle_redis_event_skips_duplicate(monkeypatch):
    from agent import session as session_mod

    async def _fake(**_kwargs):
        return {"status": "responded"}

    async def _claim(*_a, **_k):
        return False

    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    monkeypatch.setattr(session_mod, "claim_inbound_message", _claim)
    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.dup",
            "content": {"text": "hi"},
        }
    )
    assert result["status"] == "duplicate"


@pytest.mark.asyncio
async def test_handle_redis_event_releases_claim_on_empty_content(monkeypatch):
    from agent import session as session_mod

    released: list[tuple[str, str | None]] = []

    async def _claim(*_a, **_k):
        return True

    async def _release(channel, message_id):
        released.append((channel, message_id))

    async def _fake(**_kwargs):
        raise AssertionError("should not process empty content")

    monkeypatch.setattr(session_mod, "claim_inbound_message", _claim)
    monkeypatch.setattr(session_mod, "release_inbound_message", _release)
    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.empty",
            "content": {"text": "   "},
        }
    )
    assert result is None
    assert released == [("whatsapp", "wamid.empty")]


@pytest.mark.asyncio
async def test_handle_redis_event_keeps_empty_caption_image(monkeypatch):
    from agent import session as session_mod

    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return {"status": "responded", "response_text": "ok"}

    async def _claim(*_a, **_k):
        return True

    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    monkeypatch.setattr(session_mod, "claim_inbound_message", _claim)
    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.img",
            "message_type": "image",
            "content": {"text": None, "media_id": "MEDIA1", "media_type": "image/jpeg"},
        }
    )
    assert result["status"] == "responded"
    assert captured["media_id"] == "MEDIA1"
    assert captured["message_type"] == "image"


@pytest.mark.asyncio
async def test_handle_redis_event_uses_active_sender_fast_path(monkeypatch):
    from agent import session as session_mod

    handler = AsyncMock(
        return_value={
            "status": "cross_channel_handled",
            "response_text": None,
            "thread_id": "",
            "voice_session_id": "call-fast",
            "outbound_sent": False,
            "business_id": "biz-1",
        }
    )
    normal = AsyncMock()
    monkeypatch.setattr(
        session_mod, "claim_inbound_message", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(session_mod, "handle_incoming_message", normal)
    monkeypatch.setattr(
        "agent.channel_ack.acknowledge_inbound", AsyncMock()
    )
    monkeypatch.setattr("agent.progress.publish_progress", AsyncMock())
    monkeypatch.setattr(
        "voice.cross_channel.check_cross_channel_sender_route",
        AsyncMock(
            return_value={
                "call_id": "call-fast",
                "actor_type": "owner",
                "customer_id": None,
            }
        ),
    )
    monkeypatch.setattr(
        "voice.cross_channel.handle_inbound_whatsapp_if_listening",
        handler,
    )

    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "business_id": "biz-1",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.fast",
            "message_type": "document",
            "content": {
                "media_id": "MEDIA1",
                "media_type": "application/pdf",
                "filename": "services.pdf",
            },
        }
    )

    assert result["status"] == "cross_channel_handled"
    normal.assert_not_awaited()
    assert handler.await_args.kwargs["conversation_id"] == ""
    assert handler.await_args.kwargs["media_id"] == "MEDIA1"


@pytest.mark.asyncio
async def test_handle_incoming_cross_channel_skips_react(monkeypatch):
    from agent import session as session_mod
    from identity.models import IdentityResult

    identity = IdentityResult(
        business_id="biz",
        customer_id="cust",
        is_owner=False,
        is_new_customer=False,
        customer_name="A",
        is_bot_suspected=False,
    )
    monkeypatch.setattr(session_mod, "resolve_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(session_mod, "check_budget", AsyncMock(return_value=True))
    monkeypatch.setattr(session_mod, "check_message_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(session_mod, "check_bot_detection", AsyncMock(return_value=False))
    monkeypatch.setattr(session_mod, "load_business_config", AsyncMock(return_value={}))
    monkeypatch.setattr(
        session_mod,
        "get_or_create_conversation",
        AsyncMock(return_value={"conversation_id": "c1", "recent_messages": []}),
    )
    monkeypatch.setattr(
        "agent.progress.publish_progress",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "voice.cross_channel.check_cross_channel_route",
        AsyncMock(return_value={"voice_session_id": "call-1", "status": "waiting"}),
    )
    monkeypatch.setattr(
        "voice.cross_channel.handle_inbound_whatsapp_if_listening",
        AsyncMock(
            return_value={
                "status": "cross_channel_handled",
                "response_text": None,
                "thread_id": "",
                "voice_session_id": "call-1",
                "outbound_sent": False,
                "business_id": "biz",
            }
        ),
    )
    result = await session_mod.handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+91",
        message_text="",
        receiving_account_id="PNID",
        message_type="document",
        media_id="MEDIA",
    )
    assert result["status"] == "cross_channel_handled"
    assert result["voice_session_id"] == "call-1"



@pytest.mark.asyncio
async def test_handle_incoming_owner_cross_channel_skips_owner_pipeline(monkeypatch):
    from agent import session as session_mod
    from identity.models import IdentityResult

    identity = IdentityResult(
        business_id="biz",
        customer_id=None,
        is_owner=True,
        is_new_customer=False,
        customer_name="Owner",
        is_bot_suspected=False,
    )
    budget = AsyncMock(return_value=True)
    listener = AsyncMock(
        return_value={
            "voice_session_id": "owner-call",
            "actor_type": "owner",
            "status": "waiting",
        }
    )
    handler = AsyncMock(
        return_value={
            "status": "cross_channel_handled",
            "response_text": None,
            "thread_id": "",
            "voice_session_id": "owner-call",
            "outbound_sent": False,
            "business_id": "biz",
        }
    )
    monkeypatch.setattr(session_mod, "resolve_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(session_mod, "check_budget", budget)
    monkeypatch.setattr(
        session_mod,
        "get_or_create_conversation",
        AsyncMock(return_value={"conversation_id": "owner-conv", "recent_messages": []}),
    )
    monkeypatch.setattr("agent.progress.publish_progress", AsyncMock())
    monkeypatch.setattr("voice.cross_channel.check_cross_channel_route", listener)
    monkeypatch.setattr(
        "voice.cross_channel.handle_inbound_whatsapp_if_listening",
        handler,
    )

    result = await session_mod.handle_incoming_message(
        channel="whatsapp",
        external_sender_id="+919876543210",
        message_text="owner.new@example.com",
        receiving_account_id="PNID",
        message_type="text",
        external_message_id="wamid.owner",
    )

    assert result["status"] == "cross_channel_handled"
    listener.assert_awaited_once_with(
        business_id="biz",
        is_owner=True,
        customer_id=None,
        sender_external_id="+919876543210",
    )
    assert handler.await_args.kwargs["is_owner"] is True
    assert handler.await_args.kwargs["sender_external_id"] == "+919876543210"
    budget.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_redis_event_releases_claim_on_error_without_send(monkeypatch):
    from agent import session as session_mod

    released: list[tuple[str, str | None]] = []

    async def _claim(*_a, **_k):
        return True

    async def _release(channel, message_id):
        released.append((channel, message_id))

    async def _fake(**_kwargs):
        return {
            "status": "error",
            "response_text": "fallback",
            "outbound_sent": False,
        }

    monkeypatch.setattr(session_mod, "claim_inbound_message", _claim)
    monkeypatch.setattr(session_mod, "release_inbound_message", _release)
    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.err",
            "content": {"text": "hi"},
        }
    )
    assert result["status"] == "error"
    assert released == [("whatsapp", "wamid.err")]


@pytest.mark.asyncio
async def test_handle_redis_event_keeps_claim_after_responded(monkeypatch):
    from agent import session as session_mod

    released: list[tuple[str, str | None]] = []

    async def _claim(*_a, **_k):
        return True

    async def _release(channel, message_id):
        released.append((channel, message_id))

    async def _fake(**_kwargs):
        return {"status": "responded", "response_text": "ok"}

    monkeypatch.setattr(session_mod, "claim_inbound_message", _claim)
    monkeypatch.setattr(session_mod, "release_inbound_message", _release)
    monkeypatch.setattr(session_mod, "handle_incoming_message", _fake)
    result = await session_mod.handle_redis_event(
        {
            "event_type": "whatsapp_message",
            "channel": "whatsapp",
            "sender_id": "+919999999999",
            "phone_number_id": "PNID",
            "message_id": "wamid.ok",
            "content": {"text": "hi"},
        }
    )
    assert result["status"] == "responded"
    assert released == []


def test_resolve_response_text_from_ai_not_trailing_tool():
    from agent.session import _resolve_response_text

    class AIMessage:
        type = "ai"

        def __init__(self, content: str):
            self.content = content

    class ToolMessage:
        type = "tool"
        name = "create_calendar_event"
        content = '{"event_id":"e1"}'

    messages = [
        AIMessage(""),
        ToolMessage(),
        AIMessage("Created your Demo event for 3pm."),
    ]
    text = _resolve_response_text(
        messages,
        [
            {
                "tool_name": "create_calendar_event",
                "event_id": "e1",
                "summary": "Demo",
                "start": "2026-08-01T15:00:00+05:30",
            }
        ],
        ["create_calendar_event"],
    )
    assert "Created your Demo event" in text


def test_resolve_response_text_synthesizes_calendar_when_ai_empty():
    from agent.session import _resolve_response_text

    class AIMessage:
        type = "ai"
        content = ""

    class ToolMessage:
        type = "tool"
        name = "create_calendar_event"
        content = '{"event_id":"e1"}'

    messages = [AIMessage(), ToolMessage()]
    text = _resolve_response_text(
        messages,
        [
            {
                "tool_name": "create_calendar_event",
                "event_id": "e1",
                "summary": "Team sync",
                "start": "2026-08-01T15:00:00+05:30",
            }
        ],
        ["create_calendar_event"],
    )
    assert "Sorry" not in text
    assert "Team sync" in text
    assert "calendar event" in text.lower()


def test_resolve_response_text_sorry_when_no_tools():
    from agent.session import _resolve_response_text

    class AIMessage:
        type = "ai"
        content = ""

    text = _resolve_response_text([AIMessage()], [], [])
    assert "Sorry, I could not generate a reply" in text


def test_slice_current_turn_ignores_prior_tools():
    from agent.session import _collect_tool_metadata, _slice_current_turn

    class HumanMessage:
        type = "human"

        def __init__(self, content: str):
            self.content = content

    class AIMessage:
        type = "ai"

        def __init__(self, content: str = "", tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class ToolMessage:
        def __init__(self, name: str, content: str):
            self.type = "tool"
            self.name = name
            self.content = content

    messages = [
        HumanMessage("old"),
        AIMessage("", tool_calls=[{"name": "send_whatsapp_message"}]),
        ToolMessage(
            "send_whatsapp_message",
            '{"status":"sent","message_id":"old-wamid"}',
        ),
        HumanMessage("create calendar event"),
        AIMessage("", tool_calls=[{"name": "create_calendar_event"}]),
        ToolMessage(
            "create_calendar_event",
            '{"event_id":"e1","summary":"Sync"}',
        ),
    ]
    turn = _slice_current_turn(messages)
    assert turn[0].content == "create calendar event"
    names, results = _collect_tool_metadata(turn)
    assert names == ["create_calendar_event"]
    assert results[0]["tool_name"] == "create_calendar_event"
    assert "send_whatsapp_message" not in names


def test_llm_already_sent_ignores_failed_and_requires_success():
    from agent.pipeline import llm_already_sent_outbound

    assert not llm_already_sent_outbound(
        [{"tool_name": "send_whatsapp_message", "error": "boom"}]
    )
    assert llm_already_sent_outbound(
        [{"tool_name": "send_whatsapp_message", "status": "sent", "message_id": "m1"}]
    )


def test_history_send_does_not_block_pipeline_send_decision():
    """Prior-turn send_* in full history must not suppress pipeline send for this turn."""
    from agent.pipeline import llm_already_sent_outbound
    from agent.session import _collect_tool_metadata, _slice_current_turn

    class HumanMessage:
        type = "human"

        def __init__(self, content: str):
            self.content = content

    class ToolMessage:
        type = "tool"

        def __init__(self, name: str, content: str):
            self.name = name
            self.content = content

    class AIMessage:
        type = "ai"
        content = ""
        tool_calls = None

        def __init__(self, tool_calls=None):
            self.tool_calls = tool_calls

    full = [
        HumanMessage("hi"),
        AIMessage(tool_calls=[{"name": "send_whatsapp_message"}]),
        ToolMessage("send_whatsapp_message", '{"status":"sent","message_id":"old"}'),
        HumanMessage("create event again"),
        AIMessage(tool_calls=[{"name": "create_calendar_event"}]),
        ToolMessage(
            "create_calendar_event",
            '{"event_id":"e2","summary":"Demo"}',
        ),
    ]
    turn_tools, turn_results = _collect_tool_metadata(_slice_current_turn(full))
    assert turn_tools == ["create_calendar_event"]
    assert not llm_already_sent_outbound(turn_results)


@pytest.mark.asyncio
async def test_redis_bus_dispatches_concurrently(monkeypatch):
    """Second event for another sender starts while first is still in-flight."""
    import agent.redis_bus as bus

    started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def handler(event):
        mid = event["message_id"]
        order.append(f"start:{mid}")
        if mid == "a":
            started.set()
            await release_first.wait()
        order.append(f"end:{mid}")

    class FakeRedis:
        def __init__(self):
            self.sent = False
            self.acked = []
            self._events = [
                (
                    "1-0",
                    {
                        "payload": json.dumps(
                            {
                                "event_type": "whatsapp_message",
                                "business_id": "biz",
                                "sender_id": "+1",
                                "channel": "whatsapp",
                                "message_id": "a",
                            }
                        )
                    },
                ),
                (
                    "2-0",
                    {
                        "payload": json.dumps(
                            {
                                "event_type": "whatsapp_message",
                                "business_id": "biz",
                                "sender_id": "+2",
                                "channel": "whatsapp",
                                "message_id": "b",
                            }
                        )
                    },
                ),
            ]

        async def xgroup_create(self, *_a, **_k):
            return None

        async def xautoclaim(self, *_a, **_k):
            return ["0-0", []]

        async def xreadgroup(self, *_a, **_k):
            if not self.sent:
                self.sent = True
                return [("reflexo:inbound_events", self._events)]
            await asyncio.sleep(0.05)
            return []

        async def xack(self, _stream, _group, stream_id):
            self.acked.append(stream_id)
            return 1

    monkeypatch.setattr(bus, "get_redis", lambda: FakeRedis())
    stop = asyncio.Event()

    async def run():
        await bus.run_incoming_subscriber(handler, stop_event=stop, max_concurrency=8)

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), timeout=2.0)
    # Allow second message to be dispatched while first holds
    await asyncio.sleep(0.15)
    assert "start:b" in order
    release_first.set()
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert "end:a" in order
    assert "end:b" in order


def test_maybe_hedge_and_flag_helpers():
    from agent.confidence import ConfidenceAssessment, maybe_hedge_response
    from agent.escalation import format_escalation_message, should_flag_for_daily_summary

    assessment = ConfidenceAssessment(
        score=0.5, tier="MEDIUM", hedge_response=True, should_escalate=False
    )
    hedged = maybe_hedge_response("We open at 10.", assessment)
    assert hedged.startswith("Based on what I know")
    already = maybe_hedge_response("Based on what I know, we open at 10.", assessment)
    assert already.startswith("Based on what I know")

    assert should_flag_for_daily_summary({"confidence": {"tier": "MEDIUM"}}) is True
    assert should_flag_for_daily_summary({"confidence": {"tier": "HIGH"}}) is False

    body = format_escalation_message(
        customer_name="Priya",
        customer_phone="+9198",
        channel="whatsapp",
        priority="normal",
        reason="Do you have kids section?",
        context="Browsing jackets",
    )
    assert "--- REFLEXO Alert ---" in body
    assert "Priya" in body
    assert "kids section" in body


def test_enforce_response_length_voice():
    from agent.pipeline import enforce_response_length

    long = "One. Two. Three. Four."
    out = enforce_response_length(long, "whatsapp_voice")
    assert out.count(".") <= 2 or out == "One. Two."


def test_enforce_response_length_keeps_numbered_list_bodies():
    """Ordinal '1.' must not be treated as a sentence end (WhatsApp bare-'1.' bug)."""
    from agent.pipeline import enforce_response_length

    text = (
        "Fantastic, Taha! HolonCode's AI employee is now live and ready. "
        "Here's how it will work:\n"
        "1. Customers message on WhatsApp\n"
        "2. The AI replies using your knowledge\n"
        "3. It escalates hard cases to you"
    )
    out = enforce_response_length(text, "whatsapp")
    assert not out.rstrip().endswith("1.")
    assert not out.rstrip().endswith("2.")
    assert "Customers message" in out
    assert "1." in out  # list marker kept with its body


def test_enforce_response_length_strips_dangling_list_marker():
    from agent.pipeline import enforce_response_length

    text = "Thanks, I saved that. Next we need:\n1.\n"
    out = enforce_response_length(text, "whatsapp")
    assert not out.rstrip().endswith("1.")
    assert "Next we need" in out


def test_enforce_response_length_ack_plus_list_under_budget():
    from agent.pipeline import enforce_response_length

    text = (
        "Great! I have noted that. Next we need:\n"
        "1. Your business hours"
    )
    out = enforce_response_length(text, "whatsapp")
    assert "Your business hours" in out
    assert not out.rstrip().endswith("1.")


def test_whatsapp_channel_rules_ban_empty_numbered_lists():
    from agent.prompts import _get_channel_rules

    rules = _get_channel_rules("whatsapp", is_voice=False)
    assert "empty numbered" in rules.lower() or '1."' in rules or "1." in rules
    assert "plain sentences" in rules.lower()


def test_bot_thresholds_match_agent_config():
    from agent.config import AGENT_CONFIG
    from identity import bot_detection

    assert bot_detection.VELOCITY_THRESHOLD == AGENT_CONFIG["bot_velocity_threshold"]
    assert bot_detection.DEPTH_THRESHOLD == AGENT_CONFIG["bot_depth_threshold"]
    assert bot_detection.VELOCITY_WINDOW_SECONDS == AGENT_CONFIG["bot_velocity_window_s"]


def test_owner_limit_notification_template():
    from agent.error_handling import OWNER_LIMIT_NOTIFICATION

    text = OWNER_LIMIT_NOTIFICATION.format(owner_name="Ravi", limit=500)
    assert "Ravi" in text
    assert "500" in text


@pytest.mark.asyncio
async def test_queue_for_retry_indexes_business(monkeypatch):
    from agent import error_handling as eh

    calls = {"rpush": None, "sadd": None}

    class FakeRedis:
        async def rpush(self, key, value):
            calls["rpush"] = (key, value)

        async def expire(self, *_a, **_k):
            return True

        async def sadd(self, key, value):
            calls["sadd"] = (key, value)

    monkeypatch.setattr(eh, "get_redis", lambda: FakeRedis())
    await eh.queue_for_retry(
        business_id="biz-1",
        customer_id="cust-1",
        channel="whatsapp",
        message_text="hi",
        error="boom",
        external_sender_id="+91",
        receiving_account_id="PN",
    )
    assert calls["rpush"][0] == "retry_queue:biz-1"
    assert calls["sadd"][0] == "retry_queue:index"

def test_merge_customer_memory_profile_shape():
    from agent.memory import merge_customer_memory_profile

    merged = merge_customer_memory_profile(
        {"preferences": {"language": "en"}, "last_topics": ["hello"], "interaction_summary": "Prior"},
        message_text="Do you have size M jackets with COD?",
        response_text="Yes we have medium jackets and COD is available.",
        channel="whatsapp",
        tool_calls_made=[],
        ai_metadata={},
    )
    assert merged["preferences"].get("preferred_size") == "M"
    assert merged["preferences"].get("prefers_cod") is True
    assert "Do you have size M" in merged["last_topics"][-1]
    assert "Prior" in merged["interaction_summary"]
    assert merged["satisfaction_signals"]


def test_extract_token_usage():
    from agent.observability import extract_token_usage

    class Msg:
        def __init__(self, usage):
            self.usage_metadata = usage

    tokens = extract_token_usage(
        [Msg({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})]
    )
    assert tokens == {"prompt": 10, "completion": 5, "total": 15}
    assert extract_token_usage([]) is None


def test_voice_prompt_mentions_hybrid_text():
    from agent.prompts import build_customer_system_prompt

    prompt = build_customer_system_prompt(
        business_config={
            "id": "b1",
            "name": "Test",
            "persona_name": "Maya",
            "persona_tone": "friendly",
            "persona_language": ["en"],
        },
        customer_profile=None,
        channel="whatsapp_voice",
        is_voice=True,
        recent_messages=[],
        cross_channel_messages=[
            {"channel": "whatsapp", "role": "customer", "content": "here is a photo"}
        ],
    )
    assert "VOICE MODE RULES" in prompt
    assert "WhatsApp text" in prompt or "other channels" in prompt.lower()
    assert "here is a photo" in prompt


@pytest.mark.asyncio
async def test_schedule_escalation_sms_followup(monkeypatch):
    from agent import sms_followup as sf

    stored = {}

    class FakeRedis:
        async def set(self, key, value, ex=None):
            stored["set"] = (key, value, ex)

        async def zadd(self, key, mapping):
            stored["zadd"] = (key, mapping)

    monkeypatch.setattr(sf, "get_redis", lambda: FakeRedis())
    await sf.schedule_escalation_sms_followup(
        gap_id="gap-1",
        business_id="biz-1",
        owner_phone="+919999999999",
        delay_seconds=10,
    )
    assert stored["zadd"][0] == "escalation_sms:due"
    assert "gap-1" in stored["zadd"][1]
    assert "gap-1" in stored["set"][0]


@pytest.mark.asyncio
async def test_process_due_sms_skips_when_owner_responded(monkeypatch):
    from agent import sms_followup as sf

    class FakeRedis:
        async def zrangebyscore(self, *_a, **_k):
            return ["gap-1"]

        async def zrem(self, *_a, **_k):
            return 1

        async def get(self, key):
            return (
                '{"gap_id":"gap-1","business_id":"biz-1","owner_phone":"+91","due_at":1}'
            )

        async def delete(self, *_a, **_k):
            return 1

    fake = FakeRedis()
    monkeypatch.setattr(sf, "get_redis", lambda: fake)

    async def _awaiting(*_a, **_k):
        return False

    monkeypatch.setattr(sf, "_gap_still_awaiting_owner", _awaiting)
    sent = {"n": 0}

    async def _send(*_a, **_k):
        sent["n"] += 1
        return {"sent": True}

    monkeypatch.setattr(sf, "_send_owner_sms", _send)
    n = await sf.process_due_escalation_sms(now=999999)
    assert n >= 1
    assert sent["n"] == 0
