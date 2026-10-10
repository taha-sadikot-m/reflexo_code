"""Gold-retrieval specialist harness for Stage 9 quality eval."""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from typing import Any
from unittest.mock import AsyncMock, patch

from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore

from agent.guardrails import INJECTION_BLOCK_REPLY, check_input_rails
from agent.router import route_customer_message
from agent.specialist_runner import run_specialist
from tests.eval.dataset import EvalCase

_GOLD: ContextVar[list[str]] = ContextVar("eval_gold_context", default=[])

EVAL_BUSINESS_CONFIG: dict[str, Any] = {
    "id": "e9a10000-0000-4000-8000-000000000001",
    "name": "Meher Atelier",
    "vertical": "fashion",
    "persona_name": "Meher",
    "persona_tone": "warm and concise",
    "persona_language": ["en", "hi"],
    "greeting_message": "Welcome to Meher Atelier.",
    "custom_instructions": "Stay inside the retrieved facts. Do not invent prices or policies.",
    "business_hours": {
        "mon": {"open": "10:00", "close": "20:00"},
        "tue": {"open": "10:00", "close": "20:00"},
        "wed": {"open": "10:00", "close": "20:00"},
        "thu": {"open": "10:00", "close": "20:00"},
        "fri": {"open": "10:00", "close": "20:00"},
        "sat": {"open": "10:00", "close": "20:00"},
        "sun": {"closed": True},
    },
    "address": {
        "line1": "14 MG Road, Bandra West",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pincode": "400050",
    },
    "business_context": {
        "hours": "Monday to Saturday 10:00 AM to 8:00 PM; closed Sunday",
        "parking": "No dedicated lot; street parking on MG Road",
        "returns": "7-day store-credit exchange for unused tagged items; no cash refunds; sale items final",
        "membership": "Atelier Circle Rs 2499/year, 10% off full-price, early access",
        "delivery": "3-5 business days Mumbai, 7-10 PAN India, free over Rs 3000",
        "payments": "UPI, cards, Razorpay payment links",
    },
}

_PAYMENT_LINK = "https://rzp.io/eval-meher-link"


def _gold_payload() -> str:
    snippets = _GOLD.get()
    return json.dumps(
        {
            "results": [{"text": item, "score": 1.0} for item in snippets],
            "snippets": snippets,
        },
        ensure_ascii=False,
    )


def _stub(name: str, description: str, factory) -> StructuredTool:
    return StructuredTool.from_function(
        coroutine=factory,
        name=name,
        description=description,
    )


async def _search_knowledge(query: str = "") -> str:
    del query
    return _gold_payload()


async def _search_entities(query: str = "") -> str:
    del query
    return _gold_payload()


async def _list_entities(entity_type: str = "", limit: int = 50, offset: int = 0) -> str:
    del entity_type, limit, offset
    return _gold_payload()


async def _get_entity(entity_id: str = "") -> str:
    del entity_id
    snippets = _GOLD.get()
    return snippets[0] if snippets else "{}"


async def _get_business_hours() -> str:
    return (
        "Monday to Saturday 10:00 AM to 8:00 PM. Closed Sunday. "
        + _gold_payload()
    )


async def _get_dynamic_data(key: str = "") -> str:
    del key
    return _gold_payload()


async def _get_customer() -> str:
    return json.dumps({"name": None, "orders": [], "note": "New visitor"})


async def _update_customer(name: str = "", notes: str = "") -> str:
    del name, notes
    return json.dumps({"ok": True})


async def _log_gap(question: str = "") -> str:
    del question
    return json.dumps({"ok": True})


async def _escalate_to_owner(reason: str = "", summary: str = "") -> str:
    del reason, summary
    return json.dumps({"ok": True, "message": "Escalation queued for the owner."})


async def _create_payment_link(amount: float = 0, description: str = "") -> str:
    del amount, description
    return json.dumps({"ok": True, "url": _PAYMENT_LINK})


async def _check_payment_status(payment_id: str = "") -> str:
    del payment_id
    return json.dumps({"status": "created", "paid": False})


async def _no_handoff(target_agent: str, reason: str = "") -> str:
    del target_agent, reason
    return "Handoff is disabled for this evaluation. Answer using the retrieved facts."


_STUBS: dict[str, StructuredTool] = {
    "search_knowledge": _stub("search_knowledge", "Search the knowledge base.", _search_knowledge),
    "search_entities": _stub("search_entities", "Search catalog entities.", _search_entities),
    "list_entities": _stub("list_entities", "List catalog entities by type.", _list_entities),
    "get_entity": _stub("get_entity", "Get one catalog entity.", _get_entity),
    "get_business_hours": _stub("get_business_hours", "Get opening hours.", _get_business_hours),
    "get_dynamic_data": _stub("get_dynamic_data", "Get a dynamic data key.", _get_dynamic_data),
    "get_customer": _stub("get_customer", "Get the current customer.", _get_customer),
    "update_customer": _stub("update_customer", "Update customer fields.", _update_customer),
    "log_gap": _stub("log_gap", "Log a knowledge gap.", _log_gap),
    "escalate_to_owner": _stub("escalate_to_owner", "Escalate to the owner.", _escalate_to_owner),
    "create_payment_link": _stub("create_payment_link", "Create a Razorpay payment link.", _create_payment_link),
    "check_payment_status": _stub("check_payment_status", "Check a payment.", _check_payment_status),
    "handoff_to_agent": _stub("handoff_to_agent", "Handoff to another specialist.", _no_handoff),
}


async def _tools_for_names(names: list[str], business_id: str, **kwargs: Any) -> list[StructuredTool]:
    del business_id, kwargs
    tools: list[StructuredTool] = []
    for name in names:
        if name in _STUBS:
            tools.append(_STUBS[name])
        else:
            async def _noop(_name: str = name) -> str:
                return json.dumps({"ok": True, "tool": _name, "note": "eval stub"})

            tools.append(_stub(name, f"Eval stub for {name}.", _noop))
    return tools


async def route_case(case: EvalCase) -> dict[str, Any]:
    return await route_customer_message(case["input"], "whatsapp")


async def generate_response(case: EvalCase) -> dict[str, Any]:
    blocked, canned, triggers = check_input_rails(case["input"], "customer")
    if blocked:
        return {
            "actual_output": canned or INJECTION_BLOCK_REPLY,
            "blocked": True,
            "triggers": triggers,
            "specialist": None,
            "status": "blocked",
        }

    token = _GOLD.set(list(case["retrieval_context"]))
    try:
        checkpointer = MemorySaver()
        store = InMemoryStore()
        thread_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
        with (
            patch("agent.factory.tools_for_names", _tools_for_names),
            patch("agent.tools.ensure_all_mcp_sessions", AsyncMock(return_value=None)),
            patch("agent.handoff.build_handoff_tool", lambda: _STUBS["handoff_to_agent"]),
        ):
            result = await run_specialist(
                case["expected_agent"],
                case["input"],
                business_config=EVAL_BUSINESS_CONFIG,
                customer_profile=None,
                channel="whatsapp",
                is_voice=False,
                recent_messages=[],
                cross_channel_messages=[],
                checkpointer=checkpointer,
                store=store,
                thread_id=thread_id,
                conversation_id=None,
                allow_handoff=False,
            )
    finally:
        _GOLD.reset(token)

    text = str(result.get("response_text") or "").strip()
    if not text:
        text = "I do not have that information in the knowledge base."
    return {
        "actual_output": text,
        "blocked": False,
        "triggers": [],
        "specialist": result.get("specialist"),
        "status": result.get("status"),
        "tool_calls_made": result.get("tool_calls_made") or [],
    }
