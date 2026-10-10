"""Agent-side onboarding tool binding helpers."""

from __future__ import annotations

from langchain_core.tools import StructuredTool

# Onboarding-specific Core MCP tools (beyond owner set)
ONBOARDING_EXTRA_CORE_TOOLS = [
    "get_onboarding_progress",
    "update_onboarding_progress",
    "complete_onboarding",
    "generate_instagram_oauth_url",
    "generate_facebook_oauth_url",
]

# Connect helpers — also on owner allowlist; kept here so onboarding always has them
# even if owner catalog is partial during MCP startup.
ONBOARDING_CONNECT_TOOLS = [
    "connect_razorpay",
    "connect_fast2sms",
    "get_oauth_connect_url_tool",
    "list_connected_integrations",
    "get_integration_connect_instructions",
]

# Customer-support tools inherited via get_owner_tools — must not run during setup
ONBOARDING_TOOL_DENYLIST = frozenset(
    {
        "escalate_to_owner",
        "log_gap",
    }
)


async def get_onboarding_tools(
    business_id: str,
    *,
    conversation_id: str | None = None,
) -> list[StructuredTool]:
    """
    Full owner tool surface (incl. Nango actions + connect) plus onboarding extras.

    Same capabilities as owner mode so the owner can use the employee mid-setup.
    """
    from agent.tools import (
        _tools_for_names,
        get_owner_tools,
    )

    owner = await get_owner_tools(
        business_id,
        conversation_id=conversation_id,
        require_confirmation=False,
    )
    extras = await _tools_for_names(
        ONBOARDING_EXTRA_CORE_TOOLS + ONBOARDING_CONNECT_TOOLS,
        business_id,
    )
    by_name: dict[str, StructuredTool] = {
        t.name: t for t in owner if t.name not in ONBOARDING_TOOL_DENYLIST
    }
    for t in extras:
        if t.name not in ONBOARDING_TOOL_DENYLIST:
            by_name[t.name] = t
    return list(by_name.values())
