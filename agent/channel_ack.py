"""Immediate mark-as-read + typing indicator (before routing / LLM)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DM_CHANNELS = frozenset({"whatsapp", "instagram_dm", "facebook_messenger"})
_COMMENT_CHANNELS = frozenset({"instagram_comment", "facebook_comment"})


async def acknowledge_inbound(
    *,
    business_id: str,
    channel: str,
    message_id: str | None,
    sender_id: str | None,
) -> None:
    """Fail-open: never block the pipeline if Meta ack fails."""
    if not business_id or channel in _COMMENT_CHANNELS:
        return
    if channel not in _DM_CHANNELS:
        return
    try:
        from agent.tools import call_mcp_tool

        if message_id:
            await call_mcp_tool(
                "meta",
                "mark_as_read",
                {
                    "business_id": business_id,
                    "message_id": str(message_id),
                    "channel": channel,
                },
            )
        if channel in ("instagram_dm", "facebook_messenger") and sender_id:
            await call_mcp_tool(
                "meta",
                "send_typing_indicator",
                {
                    "business_id": business_id,
                    "channel": channel,
                    "recipient_id": str(sender_id),
                },
            )
    except Exception as exc:
        logger.warning(
            "Inbound ack/typing failed business=%s channel=%s: %s",
            business_id,
            channel,
            exc,
        )


def event_should_ack(event: dict[str, Any]) -> bool:
    if event.get("skip_send") or event.get("simulator"):
        return False
    if (event.get("event_type") or "") == "retry":
        return False
    return True
