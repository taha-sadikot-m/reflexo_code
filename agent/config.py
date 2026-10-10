"""Agent configuration constants (see REACT_AGENT_SPEC §13)."""

from __future__ import annotations

AGENT_CONFIG = {
    # LLM
    "model": "gemini-2.5-flash",
    "temperature": 0.3,
    "max_tokens": 1024,
    # Safety
    "max_tool_calls": 10,
    # Memory
    "checkpoint_ttl_days": 30,
    "cross_channel_context_limit": 5,
    "recent_messages_limit": 10,
    # Confidence
    "confidence_threshold_high": 0.8,
    "confidence_threshold_low": 0.4,
    # Retry
    "tool_retry_max": 1,
    "tool_retry_delay_s": 1.0,
    "llm_retry_max": 2,
    "llm_retry_delay_s": 2.0,
    # Bot detection (aligned with identity/bot_detection defaults for pipeline re-check)
    "bot_velocity_threshold": 10,
    "bot_depth_threshold": 200,
    "bot_velocity_window_s": 60,
    # Response
    "voice_max_sentences": 2,
    "text_max_sentences": 3,
}

# Stage 1 multi-agent (MULTI_AGENT_SPEC §14). Later stages add cache/guardrails/Mem0.
MULTI_AGENT_CONFIG = {
    "router_model": "gemini-2.5-flash",
    "router_max_tokens": 50,
    "router_temperature": 0,
    "router_timeout_ms": 2000,
    "broker_model": "gemini-2.5-flash",
    "broker_max_tokens": 4096,
    "broker_temperature": 0.2,
    "broker_max_delegations": 4,
    # The broker waits on a specialist plus its own LLM turns and tool hops, so
    # its budget is derived (see broker_budget_s) instead of being a flat value:
    # a fixed 30s could not outlast a 30s specialist and always timed out first.
    "broker_overhead_s": 45,
    "specialist_default_model": "gemini-2.5-flash",
    "specialist_max_tokens": 2048,
    # 15s tripped on any turn with a tool hop (a healthy one measured 14.7s),
    # and every trip used to reach the customer as an error message.
    "specialist_timeout_s": 45,
    # The owner specialists bind the whole core catalog, so their first LLM turn
    # alone measured ~28s: at 30s workflow_agent timed out before its tool result
    # came back, three delegations in a row, and the owner got no reply at all.
    "owner_specialist_timeout_s": 90,
    "max_handoffs_per_message": 2,
    "handoff_fallback_agent": "support_agent",
    "cost_alert_threshold_daily": 10.0,
    "cost_hard_limit_daily": 50.0,
}


def broker_budget_s() -> float:
    """Broker wait budget: always longer than the specialist it delegates to."""
    inner = max(
        float(MULTI_AGENT_CONFIG["specialist_timeout_s"]),
        float(MULTI_AGENT_CONFIG["owner_specialist_timeout_s"]),
    )
    return inner + float(MULTI_AGENT_CONFIG["broker_overhead_s"])


def is_multi_agent_enabled() -> bool:
    from agent.settings import get_agent_settings

    return bool(get_agent_settings().multi_agent_enabled)


def is_guardrails_enabled() -> bool:
    from agent.settings import get_agent_settings

    return bool(get_agent_settings().guardrails_enabled)


def is_semantic_cache_enabled() -> bool:
    from agent.settings import get_agent_settings

    return bool(get_agent_settings().semantic_cache_enabled)

# Events that carry an inbound user message to answer
MESSAGE_EVENT_TYPES = frozenset(
    {
        "whatsapp_message",
        "whatsapp_voice_message",
        "instagram_dm",
        "instagram_comment",
        "facebook_message",
        "facebook_comment",
    }
)

# Events that must be ignored by the agent worker
IGNORED_EVENT_TYPES = frozenset(
    {
        "whatsapp_status",
        "whatsapp_call",
        "whatsapp_call_terminate",
    }
)
