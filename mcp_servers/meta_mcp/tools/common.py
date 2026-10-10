"""Cross-channel utility tools — mark_as_read, profiles, message status."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp_servers.meta_mcp.auth.tokens import ensure_valid_token
from mcp_servers.meta_mcp.redis_keys import wa_profile_key
from mcp_servers.meta_mcp.tools.helpers import business_exists, validate_e164, wa_to_api
from mcp_servers.meta_mcp.utils.errors import MetaMCPError, tool_error
from mcp_servers.meta_mcp.utils.graph_client import graph_post
from mcp_servers.meta_mcp.utils.rate_limiter import check_rate_limit
from shared.db import tenant_connection
from shared.redis_client import get_redis

logger = logging.getLogger(__name__)


def register(mcp) -> None:
    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    async def mark_as_read(
        business_id: str,
        message_id: str,
        channel: str,
    ) -> dict[str, Any]:
        """
        Mark a message as read (WhatsApp read receipt + typing indicator).
        Instagram/Facebook are no-ops for read receipts (use send_typing_indicator).
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not message_id:
            return tool_error("INVALID_INPUT", "message_id is required")

        if channel != "whatsapp":
            return {
                "status": "success",
                "channel": channel,
                "message_id": message_id,
                "note": "Read receipts not supported on this channel",
            }

        try:
            await check_rate_limit(business_id, "whatsapp_send")
            creds = await ensure_valid_token(business_id, "whatsapp")
            phone_number_id = creds["phone_number_id"]
            body: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            }
            try:
                await graph_post(
                    f"{phone_number_id}/messages",
                    creds["access_token"],
                    body,
                    business_id=business_id,
                    channel="whatsapp",
                )
            except MetaMCPError:
                body.pop("typing_indicator", None)
                await graph_post(
                    f"{phone_number_id}/messages",
                    creds["access_token"],
                    body,
                    business_id=business_id,
                    channel="whatsapp",
                )
            return {
                "status": "success",
                "channel": "whatsapp",
                "message_id": message_id,
                "typing": True,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("mark_as_read failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        }
    )
    async def send_typing_indicator(
        business_id: str,
        channel: str,
        recipient_id: str,
    ) -> dict[str, Any]:
        """Show typing / sender_action on Instagram DM or Facebook Messenger.

        WhatsApp typing is attached to mark_as_read; this is a no-op there.
        """
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if channel in ("whatsapp", "instagram_comment", "facebook_comment"):
            return {"status": "success", "channel": channel, "note": "typing n/a"}
        if not recipient_id:
            return tool_error("INVALID_INPUT", "recipient_id is required")

        try:
            if channel == "instagram_dm":
                await check_rate_limit(business_id, "instagram_api")
                creds = await ensure_valid_token(business_id, "instagram_dm")
                page_id = creds.get("ig_account_id") or creds["channel_account_id"]
                graph_channel = "instagram_dm"
            elif channel == "facebook_messenger":
                await check_rate_limit(business_id, "facebook_send")
                creds = await ensure_valid_token(business_id, "facebook_messenger")
                page_id = creds.get("page_id") or creds["channel_account_id"]
                graph_channel = "facebook_messenger"
            else:
                return {"status": "success", "channel": channel, "note": "typing n/a"}

            await graph_post(
                f"{page_id}/messages",
                creds["access_token"],
                {
                    "recipient": {"id": recipient_id},
                    "sender_action": "typing_on",
                },
                business_id=business_id,
                channel=graph_channel,
            )
            return {
                "status": "success",
                "channel": channel,
                "typing": True,
            }
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("send_typing_indicator failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_whatsapp_profile(
        business_id: str,
        phone_number: str,
    ) -> dict[str, Any]:
        """Get WhatsApp profile name for a phone number (Contacts API)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        try:
            phone_e164 = validate_e164(phone_number)
        except MetaMCPError as exc:
            return exc.to_dict()

        cache_key = wa_profile_key(phone_e164)
        try:
            cached = await get_redis().get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

        try:
            await check_rate_limit(business_id, "whatsapp_send")
            creds = await ensure_valid_token(business_id, "whatsapp")
            phone_number_id = creds["phone_number_id"]
            data = await graph_post(
                f"{phone_number_id}/contacts",
                creds["access_token"],
                {
                    "messaging_product": "whatsapp",
                    "contacts": [wa_to_api(phone_e164)],
                },
                business_id=business_id,
                channel="whatsapp",
            )
            contacts = data.get("contacts") or []
            profile_name = None
            is_valid = False
            if contacts:
                contact = contacts[0]
                is_valid = contact.get("status") == "valid"
                profile_name = (contact.get("profile") or {}).get("name")

            result = {
                "phone_number": phone_e164,
                "profile_name": profile_name,
                "is_valid_wa_number": is_valid,
            }
            try:
                await get_redis().setex(cache_key, 3600, json.dumps(result))
            except Exception:
                pass
            return result
        except MetaMCPError as exc:
            return exc.to_dict()
        except Exception as exc:
            logger.exception("get_whatsapp_profile failed")
            return tool_error("MESSAGE_FAILED", str(exc))

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_message_status(
        business_id: str,
        message_id: str,
    ) -> dict[str, Any]:
        """Check delivery status of a sent message from local messages table."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if not message_id:
            return tool_error("ENTRY_NOT_FOUND", "message_id is required")

        try:
            async with tenant_connection(business_id) as conn:
                row = await conn.fetchrow(
                    """
                    SELECT external_message_id, status, channel, created_at,
                           error_message, ai_metadata
                    FROM messages
                    WHERE business_id = $1::uuid AND external_message_id = $2
                    LIMIT 1
                    """,
                    business_id,
                    message_id,
                )
            if not row:
                return tool_error(
                    "ENTRY_NOT_FOUND",
                    f"No message with ID {message_id} found for this business",
                )

            meta = row["ai_metadata"] or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}

            return {
                "message_id": row["external_message_id"],
                "status": row["status"],
                "channel": row["channel"],
                "sent_at": row["created_at"].isoformat() if row["created_at"] else None,
                "delivered_at": meta.get("delivered_at"),
                "read_at": meta.get("read_at"),
                "error": row["error_message"],
            }
        except Exception as exc:
            logger.exception("get_message_status failed")
            return tool_error("ENTRY_NOT_FOUND", str(exc))
