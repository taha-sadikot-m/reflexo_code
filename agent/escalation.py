"""Escalation cooldown helpers and daily-review flags."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from shared.db import tenant_connection
from shared.redis_client import get_redis
from shared.redis_keys import daily_review_key

logger = logging.getLogger(__name__)

ESCALATION_COOLDOWN_MINUTES = 30  # call-site compat only; gate is owner-response based

ESCALATION_COOLDOWN_CUSTOMER_TEXT = (
    "I have already checked with the team. They will get back to you shortly."
)

# Spec §6.3: high priority → SMS if no owner reply within 2 hours
SMS_FOLLOWUP_DELAY_SECONDS = 2 * 60 * 60


async def escalation_pending_owner_response(
    business_id: str,
    conversation_id: str,
) -> bool:
    """
    True while an escalated gap for this conversation awaits owner reply.

    Spec §6.3: max 1 escalation per conversation until the owner responds
    (no time-window expiry).
    """
    if not conversation_id:
        return False
    async with tenant_connection(business_id) as conn:
        gap = await conn.fetchrow(
            """
            SELECT 1
            FROM knowledge_gaps
            WHERE business_id = $1::uuid
              AND conversation_id = $2::uuid
              AND status IN ('open', 'escalated')
              AND escalated_at IS NOT NULL
              AND owner_responded_at IS NULL
            LIMIT 1
            """,
            business_id,
            conversation_id,
        )
    return gap is not None


async def escalation_recently_sent(
    business_id: str,
    conversation_id: str,
    *,
    cooldown_minutes: int = ESCALATION_COOLDOWN_MINUTES,
) -> bool:
    """
    Return True if escalate_to_owner must be blocked for this conversation.

    Spec §6.3: block while any escalated gap has no owner_responded_at.
    `cooldown_minutes` is unused for the gate (kept for call-site compatibility).
    """
    _ = cooldown_minutes
    return await escalation_pending_owner_response(business_id, conversation_id)


def format_escalation_message(
    *,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    channel: str = "whatsapp",
    priority: str = "normal",
    reason: str = "",
    context: str | None = None,
    customer_message: str | None = None,
) -> str:
    """Owner WhatsApp body matching REACT_AGENT_SPEC §6.2."""
    who = customer_name or "A customer"
    if customer_phone:
        who = f"{who} ({customer_phone})"
    channel_label = {
        "whatsapp": "WhatsApp",
        "whatsapp_voice": "WhatsApp Voice",
        "voice": "Voice",
        "instagram_dm": "Instagram DM",
        "instagram_comment": "Instagram Comment",
        "facebook_messenger": "Facebook Messenger",
        "facebook_comment": "Facebook Comment",
        "email": "Email",
    }.get(channel, channel.replace("_", " ").title())
    ctx = (context or customer_message or "").strip() or "No additional context."
    question = (reason or "").strip() or "Customer needs help"
    return (
        "--- REFLEXO Alert ---\n\n"
        f"Customer: {who}\n"
        f"Channel: {channel_label}\n"
        f"Priority: {priority.title()}\n\n"
        f'Question: "{question}"\n\n'
        f"Context: {ctx}\n\n"
        "Reply to this message with the answer and I will update my knowledge "
        "base automatically."
    )


def should_flag_for_daily_summary(ai_metadata: dict[str, Any]) -> bool:
    confidence = (ai_metadata or {}).get("confidence") or {}
    return confidence.get("tier") == "MEDIUM"


async def flag_conversation_for_daily_summary(
    business_id: str,
    conversation_id: str,
    *,
    day: str | None = None,
) -> None:
    """Persist a MEDIUM-confidence conversation ID for get_daily_summary."""
    if not conversation_id:
        return
    target = day or date.today().isoformat()
    key = daily_review_key(business_id, target)
    try:
        redis = get_redis()
        await redis.sadd(key, conversation_id)
        await redis.expire(key, 86400 * 3)
    except Exception as exc:
        logger.warning("Failed to flag conversation for daily summary: %s", exc)


async def list_flagged_conversations_for_day(
    business_id: str,
    day: str | None = None,
) -> list[str]:
    target = day or date.today().isoformat()
    key = daily_review_key(business_id, target)
    try:
        redis = get_redis()
        members = await redis.smembers(key)
        return sorted(str(m) for m in (members or set()))
    except Exception as exc:
        logger.debug("list flagged conversations failed: %s", exc)
        return []
