"""Persist voice turns on the canonical `messages` table (channel=voice)."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp_servers.reflexo_core.utils.db_helpers import tenant_connection

logger = logging.getLogger(__name__)


async def get_or_create_voice_conversation(
    business_id: str,
    *,
    customer_id: str | None,
    call_id: str,
) -> str:
    async with tenant_connection(business_id) as conn:
        existing = await conn.fetchrow(
            """
            SELECT id FROM conversations
            WHERE business_id = $1::uuid AND channel = 'voice'
              AND external_thread_id = $2 AND is_active = true
            LIMIT 1
            """,
            business_id,
            call_id,
        )
        if existing:
            return str(existing["id"])
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (
                business_id, customer_id, channel, external_thread_id, is_active
            ) VALUES (
                $1::uuid, $2::uuid, 'voice', $3, true
            )
            RETURNING id
            """,
            business_id,
            customer_id,
            call_id,
        )
        return str(row["id"])


async def log_voice_utterance(
    business_id: str,
    conversation_id: str,
    *,
    role: str,
    content: str,
    call_id: str,
    ai_metadata: dict[str, Any] | None = None,
) -> str | None:
    if not content or not str(content).strip():
        return None
    meta = json.dumps(ai_metadata or {"call_id": call_id})
    try:
        async with tenant_connection(business_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO messages (
                    business_id, conversation_id, role, content, content_type,
                    channel, external_message_id, ai_metadata
                ) VALUES (
                    $1::uuid, $2::uuid, $3::message_role, $4, 'text',
                    'voice', $5, $6::jsonb
                )
                RETURNING id
                """,
                business_id,
                conversation_id,
                role,
                content.strip(),
                call_id if role != "ai" else None,
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
            return str(row["id"]) if row else None
    except Exception:
        logger.exception(
            "VOICE_LOG failed business=%s conversation=%s role=%s",
            business_id,
            conversation_id,
            role,
        )
        return None
