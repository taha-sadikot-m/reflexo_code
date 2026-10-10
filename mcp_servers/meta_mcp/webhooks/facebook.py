"""Facebook Messenger and Page comment webhook parsers."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.meta_mcp.redis_keys import incoming_channel
from mcp_servers.meta_mcp.webhooks.resolve import publish_event, resolve_business_from_channel

logger = logging.getLogger(__name__)


async def handle_facebook_messenger_event(page_id: str, value: dict[str, Any]) -> None:
    """Parse Facebook Messenger DM webhook and publish to Redis."""
    business_id = await resolve_business_from_channel("facebook_messenger", page_id)
    if not business_id:
        logger.warning("No business found for FB page_id=%s", page_id)
        return

    for messaging_event in value.get("messaging", []):
        message_data = messaging_event.get("message") or {}
        if message_data.get("is_echo"):
            continue

        sender = messaging_event.get("sender") or {}
        event = {
            "event_type": "facebook_message",
            "business_id": business_id,
            "sender_id": sender.get("id"),
            "message_id": message_data.get("mid"),
            "message_type": "text",
            "content": {
                "text": message_data.get("text"),
                "attachments": message_data.get("attachments", []),
            },
            "channel": "facebook_messenger",
            "page_id": page_id,
            "timestamp": messaging_event.get("timestamp"),
            "raw": messaging_event,
        }
        await publish_event(incoming_channel(business_id), event)


async def handle_facebook_comment_event(page_id: str, value: dict[str, Any]) -> None:
    """Parse Facebook Page post comment webhook and publish to Redis."""
    if value.get("item") != "comment" or value.get("verb") != "add":
        logger.info(
            "Skipping FB feed event page=%s item=%s verb=%s",
            page_id,
            value.get("item"),
            value.get("verb"),
        )
        return

    business_id = await resolve_business_from_channel("facebook_messenger", page_id)
    if not business_id:
        logger.warning("No business found for FB page_id=%s (comment)", page_id)
        return

    from_user = value.get("from") or {}
    sender_id = from_user.get("id")
    if sender_id == page_id:
        logger.info("Skipping self-authored FB comment page=%s", page_id)
        return

    text = value.get("message") or ""
    if not str(text).strip():
        logger.info(
            "Skipping empty FB comment page=%s comment_id=%s",
            page_id,
            value.get("comment_id"),
        )
        return

    event = {
        "event_type": "facebook_comment",
        "business_id": business_id,
        "sender_id": sender_id,
        "sender_name": from_user.get("name"),
        "comment_id": value.get("comment_id"),
        "post_id": value.get("post_id"),
        "parent_id": value.get("parent_id"),
        "content": {"text": value.get("message", "")},
        "channel": "facebook_comment",
        "page_id": page_id,
        "timestamp": value.get("created_time"),
        "raw": value,
    }
    await publish_event(incoming_channel(business_id), event)
