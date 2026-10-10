"""Tenant-safe persistence for Channel Framework runtime state."""

from __future__ import annotations

import json
from typing import Any

from channel_framework.adapters.base import NormalizedMessage
from shared.db import tenant_connection


async def insert_inbound(message: NormalizedMessage) -> bool:
    async with tenant_connection(message.business_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO channel_messages (
                business_id, channel, direction, sender_id, sender_display,
                content, content_html, subject, thread_id, platform_message_id,
                attachments, metadata, processing_status
            ) VALUES (
                $1::uuid, $2, 'inbound', $3, $4, $5, $6, $7, $8, $9,
                $10::jsonb, $11::jsonb, 'processing'
            )
            ON CONFLICT (business_id, channel, platform_message_id) DO UPDATE
            SET processing_status = 'processing',
                processing_attempts = channel_messages.processing_attempts + 1,
                last_error = NULL,
                processing_started_at = now()
            WHERE channel_messages.processing_status = 'failed'
               OR (
                    channel_messages.processing_status = 'processing'
                    AND channel_messages.processing_started_at
                        < now() - interval '5 minutes'
               )
            RETURNING id
            """,
            message.business_id,
            message.channel,
            message.sender_id,
            message.sender_display,
            message.content,
            message.content_html,
            message.subject,
            message.thread_id,
            message.platform_message_id,
            json.dumps(message.attachments),
            json.dumps(message.metadata),
        )
    return bool(row)


async def finish_inbound(
    message: NormalizedMessage,
    *,
    error: str | None = None,
) -> None:
    async with tenant_connection(message.business_id) as conn:
        await conn.execute(
            """
            UPDATE channel_messages
            SET processing_status = $4,
                last_error = $5
            WHERE business_id = $1::uuid
              AND channel = $2
              AND platform_message_id = $3
              AND direction = 'inbound'
            """,
            message.business_id,
            message.channel,
            message.platform_message_id,
            "failed" if error else "completed",
            error[:2000] if error else None,
        )


async def insert_outbound(
    *,
    message: NormalizedMessage,
    content: str,
    confidence_score: float,
    send_action: str,
    platform_message_id: str,
) -> None:
    async with tenant_connection(message.business_id) as conn:
        await conn.execute(
            """
            INSERT INTO channel_messages (
                business_id, channel, direction, sender_id, sender_display,
                content, subject, thread_id, platform_message_id, in_reply_to,
                confidence_score, send_action, approved_by, metadata
            ) VALUES (
                $1::uuid, $2, 'outbound', $3, $4, $5, $6, $7, $8, $9,
                $10, $11, CASE WHEN $11 = 'queued' THEN NULL ELSE 'auto' END,
                '{}'::jsonb
            )
            """,
            message.business_id,
            message.channel,
            message.sender_id,
            message.sender_display,
            content,
            message.subject,
            message.thread_id,
            platform_message_id,
            message.platform_message_id,
            confidence_score,
            send_action,
        )
