"""Conversation tools."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from shared.redis_keys import business_config_key, usage_key
from mcp_servers.reflexo_core.utils.cache import cache_delete, get_redis
from mcp_servers.reflexo_core.utils.db_helpers import (
    business_exists,
    get_business_row,
    row_to_dict,
    service_connection,
    tenant_connection,
)
from mcp_servers.reflexo_core.utils.errors import tool_error


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_recent_conversations(
        business_id: str,
        customer_id: str | None = None,
        channel: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Get recent conversations with recent messages."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        limit = max(1, min(limit, 50))
        async with tenant_connection(business_id) as conn:
            rows = await conn.fetch(
                """
                SELECT c.*, cu.name AS customer_name
                FROM conversations c
                LEFT JOIN customers cu ON cu.id = c.customer_id
                WHERE c.business_id = $1::uuid
                  AND ($2::uuid IS NULL OR c.customer_id = $2::uuid)
                  AND ($3::text IS NULL OR c.channel::text = $3)
                ORDER BY c.last_message_at DESC NULLS LAST
                LIMIT $4
                """,
                business_id,
                customer_id,
                channel,
                limit,
            )
            conversations = []
            for r in rows:
                msgs = await conn.fetch(
                    """
                    SELECT role, content, created_at
                    FROM messages
                    WHERE conversation_id = $1::uuid
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    r["id"],
                )
                conversations.append(
                    {
                        "id": str(r["id"]),
                        "customer_name": r["customer_name"],
                        "channel": r["channel"],
                        "message_count": r["message_count"],
                        "last_message_at": r["last_message_at"].isoformat() if r["last_message_at"] else None,
                        "summary": r["summary"],
                        "recent_messages": [
                            {
                                "role": m["role"],
                                "content": m["content"],
                                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
                            }
                            for m in reversed(list(msgs))
                        ],
                    }
                )
        return {"conversations": conversations}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def get_or_create_conversation(
        business_id: str,
        channel: str,
        customer_id: str | None = None,
        external_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Get an active conversation or create a new one."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        async with tenant_connection(business_id) as conn:
            if external_thread_id:
                existing = await conn.fetchrow(
                    """
                    SELECT id FROM conversations
                    WHERE business_id = $1::uuid AND channel = $2::channel_type
                      AND external_thread_id = $3 AND is_active = true
                    LIMIT 1
                    """,
                    business_id,
                    channel,
                    external_thread_id,
                )
                if existing:
                    return {"conversation_id": str(existing["id"]), "created": False}

            if customer_id:
                existing = await conn.fetchrow(
                    """
                    SELECT id FROM conversations
                    WHERE business_id = $1::uuid AND customer_id = $2::uuid
                      AND channel = $3::channel_type AND is_active = true
                    ORDER BY last_message_at DESC
                    LIMIT 1
                    """,
                    business_id,
                    customer_id,
                    channel,
                )
                if existing:
                    return {"conversation_id": str(existing["id"]), "created": False}

            row = await conn.fetchrow(
                """
                INSERT INTO conversations (business_id, customer_id, channel, external_thread_id)
                VALUES ($1::uuid, $2::uuid, $3::channel_type, $4)
                RETURNING id
                """,
                business_id,
                customer_id,
                channel,
                external_thread_id,
            )
        return {"conversation_id": str(row["id"]), "created": True}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def log_message(
        business_id: str,
        conversation_id: str,
        role: str,
        content: str,
        channel: str,
        content_type: str = "text",
        external_message_id: str | None = None,
        ai_metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Log a message and update conversation/customer/usage counters."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        biz = await get_business_row(business_id)
        meta = json.dumps(ai_metadata or {})
        async with tenant_connection(business_id) as conn:
            conv = await conn.fetchrow(
                """
                SELECT id, customer_id FROM conversations
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                conversation_id,
                business_id,
            )
            if not conv:
                return tool_error("ENTRY_NOT_FOUND", f"Conversation {conversation_id} not found")

            # Owner/onboarding threads (customer_id IS NULL) never consume quota.
            # Customer quota: one billable unit per AI reply, not per inbound + outbound.
            billable = conv["customer_id"] is not None and role == "ai"
            if billable and biz and biz["messages_used"] >= biz["message_limit"]:
                return tool_error(
                    "MESSAGE_LIMIT_REACHED",
                    f"Business hit message limit ({biz['message_limit']})",
                )

            msg = await conn.fetchrow(
                """
                INSERT INTO messages (
                    business_id, conversation_id, role, content, content_type,
                    channel, external_message_id, ai_metadata
                ) VALUES (
                    $1::uuid, $2::uuid, $3::message_role, $4, $5,
                    $6::channel_type, $7, $8::jsonb
                ) RETURNING id
                """,
                business_id,
                conversation_id,
                role,
                content,
                content_type,
                channel,
                external_message_id,
                meta,
            )
            await conn.execute(
                """
                UPDATE conversations
                SET last_message_at = now(), message_count = message_count + 1
                WHERE id = $1::uuid
                """,
                conversation_id,
            )
            if conv["customer_id"]:
                await conn.execute(
                    """
                    UPDATE customers
                    SET last_seen_at = now(), total_messages = total_messages + 1, updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    conv["customer_id"],
                )

        if billable:
            async with service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE businesses
                    SET messages_used = messages_used + 1, updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    business_id,
                )
                await conn.execute(
                    """
                    INSERT INTO usage_events (business_id, event_type, channel, quantity)
                    VALUES ($1::uuid, 'message', $2::channel_type, 1)
                    """,
                    business_id,
                    channel,
                )

            if biz:
                cycle = biz["billing_cycle_start"]
                cycle_key = (
                    cycle.date().isoformat() if hasattr(cycle, "date") else str(cycle)[:10]
                )
                try:
                    redis = get_redis()
                    await redis.incr(usage_key(business_id, cycle_key))
                    await redis.expire(usage_key(business_id, cycle_key), 86400 * 45)
                except Exception:
                    pass
                try:
                    await cache_delete(business_config_key(business_id))
                except Exception:
                    pass

        return {"message_id": str(msg["id"]), "conversation_id": conversation_id}

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_daily_summary(
        business_id: str,
        day: str | None = None,
    ) -> dict[str, Any]:
        """Summarize conversations and gaps for a calendar day (UTC date)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        target = date.fromisoformat(day) if day else datetime.now(timezone.utc).date()
        async with tenant_connection(business_id) as conn:
            msg_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM messages
                WHERE business_id = $1::uuid AND created_at::date = $2
                """,
                business_id,
                target,
            )
            conv_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM conversations
                WHERE business_id = $1::uuid AND last_message_at::date = $2
                """,
                business_id,
                target,
            )
            open_gaps = await conn.fetchval(
                """
                SELECT COUNT(*) FROM knowledge_gaps
                WHERE business_id = $1::uuid AND status IN ('open', 'escalated')
                """,
                business_id,
            )
            top_intents = await conn.fetch(
                """
                SELECT unnest(extracted_intents) AS intent, COUNT(*) AS cnt
                FROM conversations
                WHERE business_id = $1::uuid AND last_message_at::date = $2
                GROUP BY intent ORDER BY cnt DESC LIMIT 5
                """,
                business_id,
                target,
            )

        flagged: list[str] = []
        try:
            from agent.escalation import list_flagged_conversations_for_day

            flagged = await list_flagged_conversations_for_day(
                business_id, target.isoformat()
            )
        except Exception:
            flagged = []

        return {
            "date": target.isoformat(),
            "messages": int(msg_count or 0),
            "conversations": int(conv_count or 0),
            "open_gaps": int(open_gaps or 0),
            "top_intents": [{"intent": r["intent"], "count": r["cnt"]} for r in top_intents],
            "flagged_for_review": flagged,
            "flagged_for_review_count": len(flagged),
        }
