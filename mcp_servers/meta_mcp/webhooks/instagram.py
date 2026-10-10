"""Instagram webhook parsers — DMs and comments."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.meta_mcp.redis_keys import incoming_channel
from mcp_servers.meta_mcp.webhooks.resolve import publish_event, resolve_business_from_channel

logger = logging.getLogger(__name__)


async def handle_instagram_dm_event(ig_account_id: str, value: dict[str, Any]) -> None:
    """Parse Instagram DM webhook and publish to Redis."""
    business_id = await resolve_business_from_channel("instagram_dm", ig_account_id)
    if not business_id:
        logger.warning("No business found for IG account=%s", ig_account_id)
        return

    for messaging_event in value.get("messaging", []):
        message_data = messaging_event.get("message") or {}
        if message_data.get("is_echo"):
            continue

        sender = messaging_event.get("sender") or {}
        event = {
            "event_type": "instagram_dm",
            "business_id": business_id,
            "sender_id": sender.get("id"),
            "message_id": message_data.get("mid"),
            "message_type": "text",
            "content": {
                "text": message_data.get("text"),
                "attachments": message_data.get("attachments", []),
            },
            "channel": "instagram_dm",
            "ig_account_id": ig_account_id,
            "timestamp": messaging_event.get("timestamp"),
            "raw": messaging_event,
        }
        await publish_event(incoming_channel(business_id), event)


async def handle_instagram_comment_event(ig_account_id: str, value: dict[str, Any]) -> None:
    """Parse Instagram comment webhook and publish to Redis."""
    business_id = await resolve_business_from_channel("instagram_dm", ig_account_id)
    if not business_id:
        logger.warning("No business found for IG account=%s (comment)", ig_account_id)
        return

    # Feed payloads sometimes wrap under "value" already unwrapped by handler
    comment_data = value
    if "from" not in comment_data and isinstance(value.get("value"), dict):
        comment_data = value["value"]

    from_user = comment_data.get("from") or {}
    sender_id = from_user.get("id")
    if sender_id and str(sender_id) == str(ig_account_id):
        logger.info("Skipping self-authored IG comment account=%s", ig_account_id)
        return

    text = comment_data.get("text")
    if not str(text or "").strip():
        logger.info(
            "Skipping empty IG comment account=%s comment_id=%s",
            ig_account_id,
            comment_data.get("id"),
        )
        return

    media = comment_data.get("media") or {}

    event = {
        "event_type": "instagram_comment",
        "business_id": business_id,
        "sender_id": from_user.get("id"),
        "sender_username": from_user.get("username"),
        "comment_id": comment_data.get("id"),
        "post_id": media.get("id") or comment_data.get("media_id"),
        "content": {"text": comment_data.get("text")},
        "channel": "instagram_comment",
        "ig_account_id": ig_account_id,
        "timestamp": comment_data.get("created_time") or comment_data.get("timestamp"),
        "raw": comment_data,
    }
    await publish_event(incoming_channel(business_id), event)
