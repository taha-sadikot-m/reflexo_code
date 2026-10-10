"""Main webhook payload router."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.meta_mcp.webhooks import facebook, instagram, whatsapp

logger = logging.getLogger(__name__)


async def process_webhook_payload(payload: dict[str, Any]) -> None:
    """
    Route a verified webhook payload to the appropriate parser.

    Handles both `changes` arrays and entry-level `messaging` (IG/Page).
    """
    obj = payload.get("object")
    if not obj:
        logger.warning("Webhook payload missing object field")
        return

    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))

        messaging = entry.get("messaging")
        if messaging:
            if obj == "instagram":
                await instagram.handle_instagram_dm_event(
                    account_id, {"messaging": messaging}
                )
            elif obj == "page":
                await facebook.handle_facebook_messenger_event(
                    account_id, {"messaging": messaging}
                )

        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value") or {}

            if obj == "whatsapp_business_account":
                if field == "messages":
                    await whatsapp.handle_whatsapp_event(account_id, value)
                elif field == "calls":
                    await whatsapp.handle_whatsapp_call_event(account_id, value)

            elif obj == "instagram":
                if field == "messages":
                    if "messaging" in value:
                        await instagram.handle_instagram_dm_event(account_id, value)
                    else:
                        await instagram.handle_instagram_dm_event(
                            account_id,
                            {"messaging": [value]} if value else {"messaging": []},
                        )
                elif field in ("feed", "comments"):
                    await instagram.handle_instagram_comment_event(account_id, value)
                else:
                    logger.info(
                        "Unrouted webhook object=%s field=%s account=%s",
                        obj,
                        field,
                        account_id,
                    )

            elif obj == "page":
                if field == "messages":
                    if "messaging" in value:
                        await facebook.handle_facebook_messenger_event(account_id, value)
                    else:
                        await facebook.handle_facebook_messenger_event(
                            account_id,
                            {"messaging": [value]} if value else {"messaging": []},
                        )
                elif field == "feed":
                    await facebook.handle_facebook_comment_event(account_id, value)
                else:
                    logger.info(
                        "Unrouted webhook object=%s field=%s account=%s",
                        obj,
                        field,
                        account_id,
                    )
            elif field:
                logger.info(
                    "Unrouted webhook object=%s field=%s account=%s",
                    obj,
                    field,
                    account_id,
                )
