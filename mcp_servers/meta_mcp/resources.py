"""MCP resources for Meta channel availability and rate limits."""

from __future__ import annotations

import json
from typing import Any

from mcp_servers.meta_mcp.utils.rate_limiter import RATE_LIMITS, get_rate_limit_status
from shared.db import service_connection


def register(mcp) -> None:
    @mcp.resource("meta://channels/{business_id}")
    async def business_channels_resource(business_id: str) -> str:
        """List connected Meta channels for a business."""
        async with service_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT channel, channel_display_name, channel_account_id,
                       is_active, last_synced_at, error_message
                FROM business_channels
                WHERE business_id = $1::uuid
                  AND channel IN (
                    'whatsapp', 'instagram_dm', 'facebook_messenger', 'facebook_comment'
                  )
                ORDER BY channel
                """,
                business_id,
            )

        channels: list[dict[str, Any]] = []
        for row in rows:
            channels.append(
                {
                    "channel": row["channel"],
                    "display_name": row["channel_display_name"],
                    "channel_account_id": row["channel_account_id"],
                    "is_active": row["is_active"],
                    "calling_enabled": row["channel"] == "whatsapp",
                    "last_synced_at": (
                        row["last_synced_at"].isoformat() if row["last_synced_at"] else None
                    ),
                    "error_message": row["error_message"],
                }
            )

        return json.dumps(
            {"business_id": business_id, "channels": channels},
            indent=2,
            default=str,
        )

    @mcp.resource("meta://rate-limits/{business_id}")
    async def rate_limits_resource(business_id: str) -> str:
        """Current rate limit status for Meta endpoints for this business."""
        statuses = {}
        for endpoint in RATE_LIMITS:
            statuses[endpoint] = await get_rate_limit_status(business_id, endpoint)
        return json.dumps(statuses, indent=2)
