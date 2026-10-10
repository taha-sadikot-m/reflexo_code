"""Customer intent router — rules then Gemini (MULTI_AGENT_SPEC §3)."""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from agent.config import MULTI_AGENT_CONFIG
from agent.settings import get_agent_settings

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """You are a message classifier for a business AI assistant.

Given a customer message, classify it into ONE of these categories:

- inquiry: Questions about products, services, prices, hours, policies, or general information
- sales: Purchase intent, payment requests, order placement, pricing negotiation, "I want to buy"
- support: Complaints, problems, issues, dissatisfaction, requests to talk to human/owner
- social: This is a comment on a social media post (Facebook/Instagram), not a direct message

Rules:
- Default to "inquiry" when ambiguous
- If message contains purchase/payment intent AND a complaint, choose "support" (resolve issue first)
- Greetings ("hi", "hello") → "inquiry"
- Mixed inquiry+sales ("what's the price and can I pay now") → "sales"

Respond with ONLY the category name, nothing else."""

_RULE_OVERRIDES = {
    "facebook_comment": "social_agent",
    "instagram_comment": "social_agent",
}

_SUPPORT_KEYWORDS = {
    "complaint",
    "problem",
    "issue",
    "refund",
    "cancel",
    "frustrated",
    "angry",
    "worst",
    "terrible",
    "disgusting",
    "cheat",
    "fraud",
    "shikayat",
    "problem hai",
    "paisa wapas",
    "cancel karo",
}

_SALES_KEYWORDS = {
    "buy",
    "purchase",
    "order",
    "payment",
    "pay",
    "kharidna",
    "payment link",
    "razorpay",
    "upi",
    "book",
    "reserve",
    "khareedna",
    "order karna",
    "bhejdo",
    "send kardo",
}

_CATEGORY_TO_AGENT = {
    "inquiry": "inquiry_agent",
    "sales": "sales_agent",
    "support": "support_agent",
    "social": "social_agent",
}


def _normalize_agent(name: str) -> str:
    raw = (name or "").strip().lower().replace(" ", "_")
    if raw in _CATEGORY_TO_AGENT:
        return _CATEGORY_TO_AGENT[raw]
    if raw.endswith("_agent") and raw in (
        "inquiry_agent",
        "sales_agent",
        "support_agent",
        "social_agent",
    ):
        return raw
    return "inquiry_agent"


async def route_customer_message(
    message_text: str,
    channel: str,
    message_type: str = "text",
    *,
    invoke_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Classify a customer message to a specialist.

    Returns agent (full name), confidence, method (rule|llm), latency_ms.
    """
    _ = message_type
    start = time.monotonic()

    if channel in _RULE_OVERRIDES:
        agent = _RULE_OVERRIDES[channel]
        elapsed = int((time.monotonic() - start) * 1000)
        logger.info(
            "Router rule channel=%s agent=%s latency_ms=%s",
            channel,
            agent,
            elapsed,
        )
        return {
            "agent": agent,
            "confidence": 1.0,
            "method": "rule",
            "latency_ms": elapsed,
            "token_usage": None,
        }

    text_lower = (message_text or "").lower()
    if any(kw in text_lower for kw in _SUPPORT_KEYWORDS):
        elapsed = int((time.monotonic() - start) * 1000)
        logger.info("Router keyword support latency_ms=%s", elapsed)
        return {
            "agent": "support_agent",
            "confidence": 0.85,
            "method": "rule",
            "latency_ms": elapsed,
            "token_usage": None,
        }
    if any(kw in text_lower for kw in _SALES_KEYWORDS):
        elapsed = int((time.monotonic() - start) * 1000)
        logger.info("Router keyword sales latency_ms=%s", elapsed)
        return {
            "agent": "sales_agent",
            "confidence": 0.8,
            "method": "rule",
            "latency_ms": elapsed,
            "token_usage": None,
        }

    settings = get_agent_settings()
    timeout_s = max(0.2, int(MULTI_AGENT_CONFIG["router_timeout_ms"]) / 1000)
    llm = ChatGoogleGenerativeAI(
        model=MULTI_AGENT_CONFIG["router_model"],
        google_api_key=settings.google_api_key,
        temperature=float(MULTI_AGENT_CONFIG["router_temperature"]),
        max_output_tokens=int(MULTI_AGENT_CONFIG["router_max_tokens"]),
    )
    try:
        import asyncio

        from agent.observability import extract_token_usage

        invoke_kwargs: dict[str, Any] = {}
        if invoke_config and invoke_config.get("callbacks"):
            invoke_kwargs["config"] = {"callbacks": invoke_config["callbacks"]}
        response = await asyncio.wait_for(
            llm.ainvoke(
                [
                    SystemMessage(content=ROUTER_SYSTEM_PROMPT),
                    HumanMessage(content=message_text or ""),
                ],
                **invoke_kwargs,
            ),
            timeout=timeout_s,
        )
        content = getattr(response, "content", "") or ""
        agent = _normalize_agent(str(content).splitlines()[0] if content else "")
        method = "llm"
        confidence = 0.7
        token_usage = extract_token_usage([response])
    except Exception as exc:
        logger.warning(
            "Router LLM failed (%s), default inquiry_agent: %s",
            type(exc).__name__,
            exc,
        )
        agent = "inquiry_agent"
        method = "llm"
        confidence = 0.5
        token_usage = None

    elapsed = int((time.monotonic() - start) * 1000)
    logger.info(
        "Router llm agent=%s method=%s latency_ms=%s",
        agent,
        method,
        elapsed,
    )
    return {
        "agent": agent,
        "confidence": confidence,
        "method": method,
        "latency_ms": elapsed,
        "token_usage": token_usage,
    }
