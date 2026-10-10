"""Stage 1 routing tests T1.1–T1.6 (MULTI_AGENT_SPEC / MVP Stage 1)."""

from __future__ import annotations

import json
from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.channel_tools import get_channel_tools
from agent.handoff import parse_handoff_from_tool_results, resolve_handoff_target
from agent.registry import AGENT_REGISTRY, planned_tool_names
from agent.router import route_customer_message


PRODUCT_QUESTIONS = [
    "What are your store hours?",
    "Do you have the blue kurta in size M?",
    "What's the price of the deluxe package?",
    "Where are you located?",
    "Is parking available?",
    "What services do you offer?",
    "Tell me about your return policy",
    "How long does delivery take?",
    "Are you open on Sunday?",
    "What is included in the membership?",
]

COMPLAINTS = [
    "I have a complaint about my last visit",
    "This is a terrible experience",
    "I want a refund immediately",
    "Please cancel karo this appointment",
    "problem hai with the product quality",
]

BUY_MESSAGES = [
    "I want to buy the red one",
    "Please send payment link",
    "kharidna hai yeh wala",
    "Can I purchase this today?",
    "razorpay se pay karna hai",
]


class _InquiryLLM:
    def __init__(self, *args, **kwargs):
        pass

    async def ainvoke(self, _messages):
        return SimpleNamespace(content="inquiry")


@pytest.mark.asyncio
async def test_t1_1_product_questions_route_to_inquiry(monkeypatch):
    monkeypatch.setattr("agent.router.ChatGoogleGenerativeAI", _InquiryLLM)
    for text in PRODUCT_QUESTIONS:
        result = await route_customer_message(text, "whatsapp")
        assert result["agent"] == "inquiry_agent", text
        assert result["method"] == "llm"


@pytest.mark.asyncio
async def test_t1_2_complaints_route_to_support():
    for text in COMPLAINTS:
        result = await route_customer_message(text, "whatsapp")
        assert result["agent"] == "support_agent", text
        assert result["method"] == "rule"


@pytest.mark.asyncio
async def test_t1_3_buy_intent_routes_to_sales():
    for text in BUY_MESSAGES:
        result = await route_customer_message(text, "whatsapp")
        assert result["agent"] == "sales_agent", text
        assert result["method"] == "rule"


@pytest.mark.asyncio
async def test_comment_channels_route_to_social_without_llm():
    for channel in ("instagram_comment", "facebook_comment"):
        result = await route_customer_message("I want to buy this now", channel)
        assert result["agent"] == "social_agent"
        assert result["method"] == "rule"
        assert result["confidence"] == 1.0


@pytest.mark.asyncio
async def test_t1_4_broker_delegates_analytics_and_knowledge(monkeypatch):
    from agent import broker as broker_mod

    captured: dict = {}
    specialist_calls: list[tuple[str, str, str]] = []

    async def fake_run_specialist(agent_name, message_text, **kwargs):
        specialist_calls.append((agent_name, message_text, kwargs["thread_id"]))
        return {
            "response_text": f"{agent_name} done: {message_text}",
            "tool_calls_made": ["search_knowledge"] if "analytics" in agent_name else ["upsert_entity"],
            "tool_results": [],
            "handoff_chain": [],
            "specialist": agent_name,
        }

    class DummyAgent:
        pass

    def fake_create_react_agent(**kwargs):
        captured["tools"] = {t.name: t for t in kwargs["tools"]}
        return DummyAgent()

    async def fake_invoke(agent, message_text, config, thread_id=None, **_kwargs):
        tools = captured["tools"]
        assert set(tools) == {
            "delegate_to_agent",
            "delegate_parallel",
            "reply_to_owner",
        }
        await tools["delegate_parallel"].ainvoke(
            {
                "tasks": [
                    {"agent_name": "analytics_agent", "task": "show daily summary"},
                    {
                        "agent_name": "knowledge_agent",
                        "task": "update product X price to 499",
                    },
                    {
                        "agent_name": "analytics_agent",
                        "task": "show weekly summary",
                    },
                ]
            }
        )
        await tools["reply_to_owner"].ainvoke(
            {"message": "Summary ready and product X updated."}
        )
        return {
            "messages": [
                HumanMessage(content=message_text),
                AIMessage(content="Summary ready and product X updated."),
            ]
        }

    monkeypatch.setattr(broker_mod, "run_specialist", fake_run_specialist)
    monkeypatch.setattr(broker_mod, "create_react_agent", fake_create_react_agent)
    monkeypatch.setattr(broker_mod, "ChatGoogleGenerativeAI", lambda **kwargs: object())
    monkeypatch.setattr(
        "agent.session._invoke_agent_with_llm_retries", fake_invoke
    )

    out = await broker_mod.run_broker_path(
        "show analytics and update product X",
        business_config={"id": "b1", "name": "Test Co"},
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
        checkpointer=object(),
        store=object(),
        thread_id="b1:owner:whatsapp",
    )

    assert {c[0] for c in specialist_calls} == {"analytics_agent", "knowledge_agent"}
    analytics_threads = [c[2] for c in specialist_calls if c[0] == "analytics_agent"]
    assert len(analytics_threads) == 2
    assert len(set(analytics_threads)) == 2
    assert all(":spec:analytics_agent:parallel:" in thread for thread in analytics_threads)
    assert len(out["delegates"]) == 3
    assert out["response_text"] == "Summary ready and product X updated."


@pytest.mark.asyncio
async def test_t1_5_handoff_to_sales_then_cap(monkeypatch):
    from agent import specialist_runner as runner_mod

    invokes: list[str] = []

    async def fake_create_specialist(agent_name, *args, **kwargs):
        return SimpleNamespace(name=agent_name, tools=[])

    async def fake_invoke(agent, message_text, config, thread_id=None, **_kwargs):
        name = agent.name
        invokes.append(name)
        if name == "inquiry_agent":
            payload = json.dumps(
                {
                    "__handoff__": True,
                    "target_agent": "sales_agent",
                    "reason": "purchase intent",
                }
            )
            return {
                "messages": [
                    HumanMessage(content=message_text),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "handoff_to_agent",
                                "args": {
                                    "target_agent": "sales_agent",
                                    "reason": "purchase intent",
                                },
                                "id": "1",
                            }
                        ],
                    ),
                    ToolMessage(content=payload, name="handoff_to_agent", tool_call_id="1"),
                ]
            }
        if name == "sales_agent":
            payload = json.dumps(
                {
                    "__handoff__": True,
                    "target_agent": "support_agent",
                    "reason": "need owner",
                }
            )
            return {
                "messages": [
                    HumanMessage(content=message_text),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "handoff_to_agent",
                                "args": {
                                    "target_agent": "support_agent",
                                    "reason": "need owner",
                                },
                                "id": "2",
                            }
                        ],
                    ),
                    ToolMessage(content=payload, name="handoff_to_agent", tool_call_id="2"),
                ]
            }
        if name == "support_agent":
            payload = json.dumps(
                {
                    "__handoff__": True,
                    "target_agent": "inquiry_agent",
                    "reason": "would loop",
                }
            )
            return {
                "messages": [
                    HumanMessage(content=message_text),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "handoff_to_agent",
                                "args": {
                                    "target_agent": "inquiry_agent",
                                    "reason": "would loop",
                                },
                                "id": "3",
                            }
                        ],
                    ),
                    ToolMessage(content=payload, name="handoff_to_agent", tool_call_id="3"),
                    AIMessage(content="Support will help from here."),
                ]
            }
        return {"messages": [AIMessage(content="ok")]}

    monkeypatch.setattr(runner_mod, "create_specialist_agent", fake_create_specialist)
    monkeypatch.setattr(
        "agent.session._invoke_agent_with_llm_retries", fake_invoke
    )

    out = await runner_mod.run_specialist(
        "inquiry_agent",
        "ok I want to buy it",
        business_config={"id": "b1"},
        customer_profile=None,
        channel="whatsapp",
        is_voice=False,
        recent_messages=[],
        cross_channel_messages=[],
        checkpointer=object(),
        store=object(),
        thread_id="b1:c1:whatsapp",
    )

    assert invokes == ["inquiry_agent", "sales_agent", "support_agent"]
    assert out["specialist"] == "support_agent"
    assert len(out["handoff_chain"]) == 2
    assert out["response_text"] == "Support will help from here."

    parsed = parse_handoff_from_tool_results(
        [
            {
                "tool_name": "handoff_to_agent",
                "__handoff__": True,
                "target_agent": "sales_agent",
                "reason": "purchase intent",
            }
        ]
    )
    assert parsed["target_agent"] == "sales_agent"
    assert resolve_handoff_target("sales_agent", current_agent="inquiry_agent", hop_index=0) == "sales_agent"
    assert resolve_handoff_target("sales_agent", current_agent="inquiry_agent", hop_index=2) == "support_agent"


def test_t1_6_specialist_tool_allowlist_and_counts():
    allowlist = {
        "search_knowledge",
        "search_entities",
        "get_entity",
        "get_business_hours",
        "get_dynamic_data",
        "get_customer",
        "log_gap",
        "update_customer",
        "create_payment_link",
        "check_payment_status",
        "escalate_to_owner",
        "reply_facebook_comment",
        "get_facebook_post_info",
        "get_facebook_comments",
        "get_instagram_post_info",
        "get_instagram_comments",
        "reply_instagram_comment",
        "add_knowledge",
        "update_knowledge",
        "delete_knowledge",
        "upsert_entity",
        "delete_entity",
        "list_entities",
        "process_document",
        "resolve_gap",
        "update_business_info",
        "upsert_business_context",
        "get_business_config",
        "set_dynamic_data",
        "get_daily_summary",
        "get_recent_conversations",
        "search_customers",
        "classify_contact",
        "get_contact_type",
        "list_personal_contacts",
        "create_workflow_rule",
        "list_workflow_rules",
        "update_workflow_rule",
        "delete_workflow_rule",
        "get_workflow_performance",
        "build_workflow",
        "test_workflow",
        "deploy_workflow",
        "run_workflow_scenario_test",
        "activate_workflow",
        "generate_workflow_studio_link",
        "list_active_workflows",
        "pause_workflow",
        "resume_workflow",
        "get_workflow_health",
        "handoff_to_agent",
        "connect_razorpay",
        "connect_fast2sms",
        "get_oauth_connect_url_tool",
        "list_connected_integrations",
        "get_integration_connect_instructions",
        "generate_instagram_oauth_url",
        "generate_facebook_oauth_url",
        "list_business_templates",
        "sync_whatsapp_templates",
        *get_channel_tools("whatsapp"),
        *get_channel_tools("instagram_dm"),
        *get_channel_tools("facebook_messenger"),
    }
    forbidden = {"get_or_create_conversation", "log_message"}

    expected_counts = {
        "inquiry_agent": (17, 17),
        "sales_agent": (18, 18),
        "support_agent": (15, 15),
        "social_agent": (11, 11),
        "knowledge_agent": (10, 10),
        "config_agent": (8, 8),
        "analytics_agent": (4, 4),
        "workflow_agent": (17, 17),
        "integrations_agent": (7, 7),
        "custom_tools_agent": (3, 3),
    }

    assert set(AGENT_REGISTRY) == {
        "inquiry_agent",
        "sales_agent",
        "support_agent",
        "social_agent",
        "knowledge_agent",
        "config_agent",
        "analytics_agent",
        "workflow_agent",
        "integrations_agent",
        "custom_tools_agent",
    }

    for name in AGENT_REGISTRY:
        channel = "instagram_comment" if name == "social_agent" else "whatsapp"
        names = planned_tool_names(name, channel)
        extra = set(names) - allowlist
        assert not extra, f"{name} unexpected tools {extra}"
        assert not (set(names) & forbidden), f"{name} has forbidden tools"
        lo, hi = expected_counts[name]
        assert lo <= len(names) <= hi, f"{name} count {len(names)} not in [{lo},{hi}]"

    social = planned_tool_names("social_agent", "instagram_comment")
    assert "send_whatsapp_message" not in social
    assert "handoff_to_agent" not in social
    assert "reply_instagram_comment" in social
    assert "list_entities" in social
    assert "get_entity" in social
    assert "get_instagram_post_info" in social
    assert "get_instagram_comments" in social

    inquiry = planned_tool_names("inquiry_agent", "whatsapp")
    assert "handoff_to_agent" in inquiry
    assert "list_entities" in inquiry
    assert "log_gap" not in inquiry
    assert "send_whatsapp_message" in inquiry
    assert "get_or_create_conversation" not in inquiry

    sales = planned_tool_names("sales_agent", "whatsapp")
    assert "list_entities" in sales
