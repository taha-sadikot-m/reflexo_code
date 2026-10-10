"""Channel-aware Meta send/reply tool names (MULTI_AGENT_SPEC §7.1).

Pipeline already creates conversations and logs messages, so INFRA_TOOLS
(get_or_create_conversation / log_message) are not injected on specialists.
"""

from __future__ import annotations

CHANNEL_SEND_TOOLS: dict[str, list[str]] = {
    "whatsapp": [
        "send_whatsapp_message",
        "send_whatsapp_template",
        "send_whatsapp_interactive",
        "send_whatsapp_media",
        "send_whatsapp_carousel",
        "send_whatsapp_location",
        "send_whatsapp_catalog",
        "send_whatsapp_flow",
        "mark_as_read",
    ],
    "instagram_dm": [
        "send_instagram_dm",
        "mark_as_read",
    ],
    "facebook_messenger": [
        "send_facebook_message",
        "mark_as_read",
    ],
    "facebook_comment": [],
    "instagram_comment": [],
}


def get_channel_tools(channel: str) -> list[str]:
    """Messaging tools for this inbound channel (not the full Meta catalog)."""
    return list(CHANNEL_SEND_TOOLS.get(channel, []))
