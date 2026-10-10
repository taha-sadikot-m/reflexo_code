"""Knowledge gaps and escalation tools."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.settings import get_settings
from mcp_servers.reflexo_core.utils.db_helpers import (
    business_exists,
    get_business_row,
    tenant_connection,
    vec_literal,
)
from mcp_servers.reflexo_core.utils.errors import tool_error

logger = logging.getLogger(__name__)


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def log_gap(
        business_id: str,
        question: str,
        context: str | None = None,
        conversation_id: str | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Log a knowledge gap when the AI cannot answer."""
        if not question or not question.strip():
            return tool_error("EMPTY_QUERY", "question is empty")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        try:
            embedding = await embed(question.strip())
        except Exception as exc:
            return tool_error("INVALID_INPUT", f"Embedding failed: {exc}")
        vec = vec_literal(embedding)

        async with tenant_connection(business_id) as conn:
            similar = await conn.fetchrow(
                """
                SELECT id, occurrence_count FROM knowledge_gaps
                WHERE business_id = $1::uuid AND status = 'open'
                  AND embedding IS NOT NULL
                  AND embedding <=> $2::vector < 0.15
                ORDER BY embedding <=> $2::vector
                LIMIT 1
                """,
                business_id,
                vec,
            )
            if similar:
                await conn.execute(
                    """
                    UPDATE knowledge_gaps
                    SET occurrence_count = occurrence_count + 1,
                        last_occurred_at = now(),
                        updated_at = now()
                    WHERE id = $1::uuid
                    """,
                    similar["id"],
                )
                return {
                    "gap_id": str(similar["id"]),
                    "status": "open",
                    "occurrence_count": similar["occurrence_count"] + 1,
                    "deduplicated": True,
                }

            row = await conn.fetchrow(
                """
                INSERT INTO knowledge_gaps (
                    business_id, question, context, conversation_id, customer_id, embedding
                ) VALUES ($1::uuid, $2, $3, $4::uuid, $5::uuid, $6::vector)
                RETURNING id, occurrence_count
                """,
                business_id,
                question.strip(),
                context,
                conversation_id,
                customer_id,
                vec,
            )
        return {
            "gap_id": str(row["id"]),
            "status": "open",
            "occurrence_count": row["occurrence_count"],
            "deduplicated": False,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def escalate_to_owner(
        business_id: str,
        reason: str,
        customer_id: str | None = None,
        conversation_id: str | None = None,
        priority: str = "normal",
        channel: str | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Escalate to the business owner via WhatsApp (or log if Meta MCP unavailable)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        # Soft cooldown if conversation_id provided (agent wrapper is primary gate)
        if conversation_id:
            try:
                from agent.escalation import (
                    ESCALATION_COOLDOWN_CUSTOMER_TEXT,
                    escalation_recently_sent,
                )

                if await escalation_recently_sent(business_id, conversation_id):
                    return {
                        "status": "pending_owner",
                        "escalated": False,
                        "agent_guidance": (
                            "An escalation is already waiting for the owner. "
                            f'Tell the customer: "{ESCALATION_COOLDOWN_CUSTOMER_TEXT}"'
                        ),
                        "customer_message": ESCALATION_COOLDOWN_CUSTOMER_TEXT,
                    }
            except Exception as exc:
                logger.debug("Escalation cooldown check skipped: %s", exc)

        biz = await get_business_row(business_id)
        owner_phone = biz["owner_phone"]

        customer_name = None
        customer_phone = None
        if customer_id:
            async with tenant_connection(business_id) as conn:
                cust = await conn.fetchrow(
                    """
                    SELECT name, phone FROM customers
                    WHERE id = $1::uuid AND business_id = $2::uuid
                    """,
                    customer_id,
                    business_id,
                )
            if cust:
                customer_name = cust["name"]
                customer_phone = cust["phone"]

        from agent.escalation import format_escalation_message

        message = format_escalation_message(
            customer_name=customer_name,
            customer_phone=customer_phone,
            channel=channel or "whatsapp",
            priority=priority or "normal",
            reason=reason,
            context=context,
        )

        gap_id = None
        async with tenant_connection(business_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO knowledge_gaps (
                    business_id, question, context, conversation_id, customer_id,
                    status, escalated_at, escalation_channel
                ) VALUES (
                    $1::uuid, $2, $3, $4::uuid, $5::uuid,
                    'escalated', now(), 'whatsapp'
                ) RETURNING id
                """,
                business_id,
                reason,
                message,
                conversation_id,
                customer_id,
            )
            gap_id = str(row["id"])

        settings = get_settings()
        if settings.meta_mcp_url:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.post(
                        f"{settings.meta_mcp_url.rstrip('/')}/tools/send_whatsapp_message",
                        json={
                            "business_id": business_id,
                            "to": owner_phone,
                            "text": message,
                        },
                    )
                status = "sent"
            except Exception as exc:
                logger.warning("Meta MCP escalate failed: %s", exc)
                status = "logged"
        else:
            status = "logged"

        sms_scheduled = False
        if priority == "high" and gap_id and owner_phone:
            try:
                from agent.sms_followup import schedule_escalation_sms_followup

                await schedule_escalation_sms_followup(
                    gap_id=gap_id,
                    business_id=business_id,
                    owner_phone=owner_phone,
                )
                sms_scheduled = True
            except Exception as exc:
                logger.warning(
                    "Failed to schedule high-priority SMS follow-up for %s: %s",
                    gap_id,
                    exc,
                )

        return {
            "escalation_id": gap_id,
            "status": status,
            "channel": "whatsapp",
            "owner_phone": owner_phone,
            "sms_followup_scheduled": sms_scheduled,
            "message": (
                "Escalation sent to owner via WhatsApp"
                if status == "sent"
                else "Escalation logged (Meta MCP not configured); notify owner manually"
            ),
            "body": message,
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def resolve_gap(
        business_id: str,
        gap_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """Resolve a knowledge gap and create a KB entry."""
        if not answer or not answer.strip():
            return tool_error("EMPTY_ANSWER", "answer is empty")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        async with tenant_connection(business_id) as conn:
            gap = await conn.fetchrow(
                "SELECT * FROM knowledge_gaps WHERE id = $1::uuid AND business_id = $2::uuid",
                gap_id,
                business_id,
            )
            if not gap:
                return tool_error("ENTRY_NOT_FOUND", f"Gap {gap_id} not found")

        # Create knowledge via same embedding path as add_knowledge
        from mcp_servers.reflexo_core.search.embeddings import embed as embed_fn
        from mcp_servers.reflexo_core.utils.db_helpers import vec_literal as vl
        from mcp_servers.reflexo_core.utils.cache import cache_delete_prefix

        text = f"{gap['question']} {answer}".strip()
        try:
            embedding = await embed_fn(text)
        except Exception as exc:
            return tool_error("INVALID_INPUT", f"Embedding failed: {exc}")
        vec = vl(embedding)

        async with tenant_connection(business_id) as conn:
            kb = await conn.fetchrow(
                """
                INSERT INTO knowledge_entries (business_id, question, answer, category, embedding, source)
                VALUES ($1::uuid, $2, $3, 'general', $4::vector, 'ai_learned')
                RETURNING id
                """,
                business_id,
                gap["question"],
                answer.strip(),
                vec,
            )
            await conn.execute(
                """
                UPDATE knowledge_gaps
                SET status = 'resolved', answer = $1, knowledge_entry_id = $2::uuid,
                    owner_responded_at = now(), updated_at = now()
                WHERE id = $3::uuid
                """,
                answer.strip(),
                kb["id"],
                gap_id,
            )

        await cache_delete_prefix(f"kb_cache:{business_id}:")
        return {
            "gap_id": gap_id,
            "status": "resolved",
            "knowledge_entry_id": str(kb["id"]),
            "message": "Gap resolved and knowledge base updated",
        }
