"""MCP resources."""

from __future__ import annotations

import json

from mcp_servers.reflexo_core.tools.config import get_business_config_impl
from mcp_servers.reflexo_core.utils.db_helpers import tenant_connection


async def get_quick_stats_impl(business_id: str) -> dict:
    async with tenant_connection(business_id) as conn:
        messages_today = await conn.fetchval(
            """
            SELECT COUNT(*) FROM messages
            WHERE business_id = $1::uuid AND created_at::date = CURRENT_DATE
            """,
            business_id,
        )
        active_conversations = await conn.fetchval(
            """
            SELECT COUNT(*) FROM conversations
            WHERE business_id = $1::uuid AND is_active = true
            """,
            business_id,
        )
        pending_gaps = await conn.fetchval(
            """
            SELECT COUNT(*) FROM knowledge_gaps
            WHERE business_id = $1::uuid AND status IN ('open', 'escalated')
            """,
            business_id,
        )
    return {
        "messages_today": int(messages_today or 0),
        "active_conversations": int(active_conversations or 0),
        "pending_gaps": int(pending_gaps or 0),
    }


def register(mcp) -> None:
    @mcp.resource("business://config/{business_id}")
    async def business_config_resource(business_id: str) -> str:
        """Full business configuration for system prompt building."""
        config = await get_business_config_impl(business_id)
        return json.dumps(config or {"error": "not_found"}, indent=2)

    @mcp.resource("business://hours/{business_id}")
    async def business_hours_resource(business_id: str) -> str:
        """Current business hours."""
        config = await get_business_config_impl(business_id)
        hours = (config or {}).get("business_hours", {})
        return json.dumps(hours, indent=2)

    @mcp.resource("business://stats/{business_id}")
    async def business_stats_resource(business_id: str) -> str:
        """Quick stats: messages today, active conversations, pending gaps."""
        stats = await get_quick_stats_impl(business_id)
        return json.dumps(stats, indent=2)
