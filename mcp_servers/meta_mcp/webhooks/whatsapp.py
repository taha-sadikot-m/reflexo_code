"""WhatsApp webhook parsers — messages, statuses, and calling events."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp_servers.meta_mcp.redis_keys import incoming_channel, status_channel
from mcp_servers.meta_mcp.webhooks.resolve import publish_event, resolve_business_from_channel
from shared.db import service_connection

logger = logging.getLogger(__name__)


def extract_whatsapp_content(message: dict[str, Any]) -> dict[str, Any]:
    """Extract structured content from a WhatsApp message based on type."""
    msg_type = message.get("type")
    content: dict[str, Any] = {
        "text": None,
        "media_id": None,
        "media_type": None,
        "filename": None,
        "location": None,
        "contacts": None,
        "interactive": None,
        "reaction": None,
        "context": message.get("context"),
    }

    if msg_type == "text":
        content["text"] = message.get("text", {}).get("body")
    elif msg_type == "image":
        img = message.get("image", {})
        content["media_id"] = img.get("id")
        content["media_type"] = img.get("mime_type")
        content["text"] = img.get("caption")
    elif msg_type == "document":
        doc = message.get("document", {})
        content["media_id"] = doc.get("id")
        content["media_type"] = doc.get("mime_type")
        content["filename"] = doc.get("filename")
        content["text"] = doc.get("caption")
    elif msg_type == "audio":
        audio = message.get("audio", {})
        content["media_id"] = audio.get("id")
        content["media_type"] = audio.get("mime_type")
    elif msg_type == "video":
        video = message.get("video", {})
        content["media_id"] = video.get("id")
        content["media_type"] = video.get("mime_type")
        content["text"] = video.get("caption")
    elif msg_type == "sticker":
        sticker = message.get("sticker", {})
        content["media_id"] = sticker.get("id")
        content["media_type"] = sticker.get("mime_type")
    elif msg_type == "location":
        content["location"] = message.get("location")
    elif msg_type == "contacts":
        content["contacts"] = message.get("contacts")
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        content["interactive"] = {"type": interactive.get("type"), "reply": reply}
        content["text"] = reply.get("title")
    elif msg_type == "reaction":
        content["reaction"] = message.get("reaction")
    elif msg_type == "button":
        # Legacy quick-reply button
        btn = message.get("button", {})
        content["text"] = btn.get("text") or btn.get("payload")
        content["interactive"] = {"type": "button", "reply": btn}

    return content


async def update_message_status(
    external_message_id: str,
    status: str,
    errors: list[Any] | None = None,
) -> None:
    """Update messages.status / error_message by external_message_id."""
    if not external_message_id:
        return
    error_text = None
    if errors:
        try:
            error_text = json.dumps(errors, default=str)[:2000]
        except Exception:
            error_text = str(errors)[:2000]

    try:
        async with service_connection() as conn:
            await conn.execute(
                """
                UPDATE messages
                SET status = $1,
                    error_message = COALESCE($2, error_message),
                    ai_metadata = COALESCE(ai_metadata, '{}'::jsonb)
                        || jsonb_build_object(
                            'last_status', $1::text,
                            'status_updated_at', now()::text
                        )
                WHERE external_message_id = $3
                """,
                status,
                error_text if status == "failed" else None,
                external_message_id,
            )
    except Exception as exc:
        logger.error("update_message_status failed for %s: %s", external_message_id, exc)


async def handle_whatsapp_event(waba_id: str, value: dict[str, Any]) -> None:
    """Parse WhatsApp webhook payload and publish to Redis."""
    phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
    if not phone_number_id:
        logger.warning("WhatsApp event missing phone_number_id (waba=%s)", waba_id)
        return

    business_id = await resolve_business_from_channel("whatsapp", phone_number_id)
    if not business_id:
        logger.warning("No business found for phone_number_id=%s", phone_number_id)
        return

    for message in value.get("messages", []):
        sender_phone = message.get("from", "")
        sender_id = sender_phone if str(sender_phone).startswith("+") else f"+{sender_phone}"
        event = {
            "event_type": "whatsapp_message",
            "business_id": business_id,
            "sender_id": sender_id,
            "message_id": message.get("id"),
            "message_type": message.get("type"),
            "content": extract_whatsapp_content(message),
            "channel": "whatsapp",
            "phone_number_id": phone_number_id,
            "waba_id": waba_id,
            "timestamp": message.get("timestamp"),
            "raw": message,
        }
        await publish_event(incoming_channel(business_id), event)

    for status in value.get("statuses", []):
        status_event = {
            "event_type": "whatsapp_status",
            "business_id": business_id,
            "message_id": status.get("id"),
            "status": status.get("status"),
            "recipient": status.get("recipient_id"),
            "timestamp": status.get("timestamp"),
            "errors": status.get("errors", []),
        }
        await publish_event(status_channel(business_id), status_event)
        await update_message_status(
            status.get("id", ""),
            status.get("status", ""),
            status.get("errors"),
        )


async def get_phone_number_id_from_number(business_number: str) -> str | None:
    """Resolve phone_number_id from a business WhatsApp display number (best-effort)."""
    cleaned = business_number.lstrip("+").replace(" ", "")
    async with service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT channel_account_id
            FROM business_channels
            WHERE channel = 'whatsapp'
              AND is_active = true
              AND (
                channel_account_id = $1
                OR channel_display_name = $2
                OR channel_display_name = $3
              )
            LIMIT 1
            """,
            cleaned,
            business_number,
            f"+{cleaned}",
        )
    return str(row["channel_account_id"]) if row else None


async def post_call_cleanup(business_id: str, call_id: str) -> None:
    logger.info("post_call_cleanup business=%s call_id=%s", business_id, call_id)


async def handle_whatsapp_call_event(waba_id: str, value: dict[str, Any]) -> None:
    """Handle WhatsApp Business Calling API webhook events (SDP + LiveKit)."""
    from voice.call_handler import process_call_payload

    await process_call_payload(waba_id, value)
