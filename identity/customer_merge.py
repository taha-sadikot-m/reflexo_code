"""Customer identity merging across channels."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from identity.cache import IdentityCache
from shared.db import get_pool
from shared.redis_keys import identity_key

logger = logging.getLogger(__name__)


async def merge_customers(
    business_id: str,
    primary_customer_id: str,
    secondary_customer_id: str,
    merge_reason: str = "manual",
    merged_by: str = "system",
) -> dict:
    """Merge secondary customer into primary. Soft-deletes secondary."""
    if primary_customer_id == secondary_customer_id:
        raise ValueError("Cannot merge a customer with itself")

    pool = get_pool()
    cache = IdentityCache()

    async with pool.acquire() as conn:
        async with conn.transaction():
            primary = await conn.fetchrow(
                "SELECT id, name, phone, email, preferences, tags, notes, "
                "total_messages, total_orders, total_spent "
                "FROM customers WHERE id = $1::uuid AND business_id = $2::uuid",
                primary_customer_id,
                business_id,
            )
            secondary = await conn.fetchrow(
                "SELECT id, name, phone, email, preferences, tags, notes, "
                "total_messages, total_orders, total_spent "
                "FROM customers WHERE id = $1::uuid AND business_id = $2::uuid",
                secondary_customer_id,
                business_id,
            )

            if not primary:
                raise ValueError(f"Primary customer {primary_customer_id} not found")
            if not secondary:
                raise ValueError(f"Secondary customer {secondary_customer_id} not found")

            secondary_identities = await conn.fetch(
                "SELECT channel, external_id FROM identity_map "
                "WHERE customer_id = $1::uuid AND business_id = $2::uuid",
                secondary_customer_id,
                business_id,
            )

            identities_moved = await conn.execute(
                """
                UPDATE identity_map
                SET customer_id = $3::uuid
                WHERE customer_id = $1::uuid
                  AND business_id = $2::uuid
                  AND NOT EXISTS (
                      SELECT 1 FROM identity_map im2
                      WHERE im2.customer_id = $3::uuid
                        AND im2.business_id = $2::uuid
                        AND im2.channel = identity_map.channel
                        AND im2.external_id = identity_map.external_id
                  )
                """,
                secondary_customer_id,
                business_id,
                primary_customer_id,
            )
            identities_moved_count = int(identities_moved.split()[-1])

            await conn.execute(
                "DELETE FROM identity_map WHERE customer_id = $1::uuid AND business_id = $2::uuid",
                secondary_customer_id,
                business_id,
            )

            conversations_moved = await conn.execute(
                "UPDATE conversations SET customer_id = $3::uuid "
                "WHERE customer_id = $1::uuid AND business_id = $2::uuid",
                secondary_customer_id,
                business_id,
                primary_customer_id,
            )
            conversations_moved_count = int(conversations_moved.split()[-1])

            merged_phone = primary["phone"] or secondary["phone"]
            merged_email = primary["email"] or secondary["email"]
            merged_name = primary["name"] or secondary["name"]

            primary_prefs = primary["preferences"] or {}
            secondary_prefs = secondary["preferences"] or {}
            if isinstance(primary_prefs, str):
                primary_prefs = json.loads(primary_prefs)
            if isinstance(secondary_prefs, str):
                secondary_prefs = json.loads(secondary_prefs)
            merged_prefs = {**(secondary_prefs or {}), **(primary_prefs or {})}

            primary_tags = set(primary["tags"] or [])
            secondary_tags = set(secondary["tags"] or [])
            merged_tags = list(primary_tags | secondary_tags)

            merged_notes = primary["notes"] or ""
            if secondary["notes"]:
                if merged_notes:
                    merged_notes += f"\n\n--- Merged from customer {secondary_customer_id} ---\n"
                merged_notes += secondary["notes"]

            merged_messages = (primary["total_messages"] or 0) + (
                secondary["total_messages"] or 0
            )
            merged_orders = (primary["total_orders"] or 0) + (secondary["total_orders"] or 0)
            merged_spent = (primary["total_spent"] or 0) + (secondary["total_spent"] or 0)

            await conn.execute(
                """
                UPDATE customers SET
                    name = $3,
                    phone = $4,
                    email = $5,
                    preferences = $6::jsonb,
                    tags = $7,
                    notes = $8,
                    total_messages = $9,
                    total_orders = $10,
                    total_spent = $11,
                    updated_at = NOW()
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                primary_customer_id,
                business_id,
                merged_name,
                merged_phone,
                merged_email,
                json.dumps(merged_prefs),
                merged_tags,
                merged_notes or None,
                merged_messages,
                merged_orders,
                merged_spent,
            )

            await conn.execute(
                """
                UPDATE customers SET
                    name = $3,
                    tags = array_append(COALESCE(tags, '{}'), $4),
                    notes = $5,
                    updated_at = NOW()
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                secondary_customer_id,
                business_id,
                f"[MERGED into {primary_customer_id}] {secondary['name'] or 'Unknown'}",
                "merged_into:" + primary_customer_id,
                f"Merged into {primary_customer_id} at {datetime.now(timezone.utc).isoformat()} "
                f"reason={merge_reason} by={merged_by}",
            )

    for ident in secondary_identities:
        await cache.delete(
            identity_key(ident["channel"], ident["external_id"], business_id)
        )

    logger.info(
        "Merged customers: primary=%s secondary=%s reason=%s identities=%s convs=%s",
        primary_customer_id,
        secondary_customer_id,
        merge_reason,
        identities_moved_count,
        conversations_moved_count,
    )

    return {
        "status": "merged",
        "primary_customer_id": primary_customer_id,
        "secondary_customer_id": secondary_customer_id,
        "identities_moved": identities_moved_count,
        "conversations_moved": conversations_moved_count,
        "merge_reason": merge_reason,
    }


async def auto_merge_on_email_match(
    business_id: str,
    customer_id: str,
    email: str,
) -> Optional[dict]:
    """If another customer in this business has the same email, merge into older record."""
    if not email:
        return None
    email = email.lower().strip()
    pool = get_pool()
    other = await pool.fetchrow(
        """
        SELECT id::text, first_seen_at
        FROM customers
        WHERE business_id = $1::uuid
          AND lower(email) = $2
          AND id != $3::uuid
          AND (tags IS NULL OR NOT ('merged_into:' || $3 = ANY(tags)))
          AND name NOT LIKE '[MERGED%%'
        ORDER BY first_seen_at ASC
        LIMIT 1
        """,
        business_id,
        email,
        customer_id,
    )
    if not other:
        return None

    current = await pool.fetchrow(
        "SELECT first_seen_at FROM customers WHERE id = $1::uuid",
        customer_id,
    )
    if current and other["first_seen_at"] <= current["first_seen_at"]:
        primary, secondary = other["id"], customer_id
    else:
        primary, secondary = customer_id, other["id"]

    return await merge_customers(
        business_id,
        primary,
        secondary,
        merge_reason="auto_email_match",
        merged_by="system",
    )
