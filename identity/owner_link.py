"""Link Instagram/Facebook external IDs to the business owner for recognition."""

from __future__ import annotations

import logging
from typing import Any

from identity.cache import IdentityCache
from identity.models import Channel
from shared.db import get_pool
from shared.redis_keys import identity_key, owner_check_key

logger = logging.getLogger(__name__)

_OWNER_LINKABLE_CHANNELS = frozenset(
    {
        Channel.INSTAGRAM_DM,
        Channel.INSTAGRAM_COMMENT,
        Channel.FACEBOOK_MESSENGER,
        Channel.FACEBOOK_COMMENT,
    }
)


async def link_owner_channel_identity(
    business_id: str,
    channel: str,
    external_id: str,
    *,
    display_name: str | None = None,
) -> dict[str, Any]:
    """
    Upsert identity_map so IG/FB sender_external_id resolves as the owner.

    Finds or creates a customers row whose phone/email matches businesses.owner_phone
    / owner_email, then links (business_id, channel, external_id) to that customer.
    Invalidates owner_check + identity Redis keys for this triple.
    """
    external_id = (external_id or "").strip()
    if not business_id or not external_id:
        raise ValueError("business_id and external_id are required")

    try:
        ch = Channel(channel)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported channel {channel!r}; use one of: "
            + ", ".join(sorted(c.value for c in _OWNER_LINKABLE_CHANNELS))
        ) from exc

    if ch not in _OWNER_LINKABLE_CHANNELS:
        raise ValueError(
            f"Channel {ch.value} cannot be linked for owner recognition; "
            f"use Instagram or Facebook channels"
        )

    pool = get_pool()
    biz = await pool.fetchrow(
        """
        SELECT id::text, owner_phone, owner_email, owner_name
        FROM businesses
        WHERE id = $1::uuid AND is_active = true
        """,
        business_id,
    )
    if not biz:
        raise ValueError(f"No active business found with ID {business_id}")

    owner_phone = biz["owner_phone"]
    owner_email = biz["owner_email"]
    if not owner_phone and not owner_email:
        raise ValueError(
            "Business has neither owner_phone nor owner_email; cannot link owner identity"
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            customer_id = await conn.fetchval(
                """
                SELECT id::text
                FROM customers
                WHERE business_id = $1::uuid
                  AND (
                      ($2::text IS NOT NULL AND phone = $2)
                      OR ($3::text IS NOT NULL AND email = $3)
                  )
                ORDER BY last_seen_at DESC NULLS LAST
                LIMIT 1
                """,
                business_id,
                owner_phone,
                owner_email,
            )
            created_customer = False
            if not customer_id:
                customer_id = await conn.fetchval(
                    """
                    INSERT INTO customers (
                        business_id, name, phone, email,
                        primary_channel, first_seen_at, last_seen_at
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4, $5::channel_type, NOW(), NOW()
                    )
                    RETURNING id::text
                    """,
                    business_id,
                    display_name or biz["owner_name"] or "Owner",
                    owner_phone,
                    owner_email,
                    ch.value,
                )
                created_customer = True

            await conn.execute(
                """
                INSERT INTO identity_map (
                    business_id, customer_id, channel, external_id, display_name
                )
                VALUES ($1::uuid, $2::uuid, $3::channel_type, $4, $5)
                ON CONFLICT (business_id, channel, external_id)
                DO UPDATE SET
                    customer_id = EXCLUDED.customer_id,
                    display_name = COALESCE(EXCLUDED.display_name, identity_map.display_name)
                """,
                business_id,
                customer_id,
                ch.value,
                external_id,
                display_name,
            )

    cache = IdentityCache()
    await cache.delete(owner_check_key(business_id, ch.value, external_id))
    await cache.delete(identity_key(ch.value, external_id, business_id))

    logger.info(
        "Linked owner identity business=%s channel=%s external_id=%s customer=%s created=%s",
        business_id,
        ch.value,
        external_id,
        customer_id,
        created_customer,
    )
    return {
        "status": "linked",
        "business_id": business_id,
        "channel": ch.value,
        "external_id": external_id,
        "customer_id": customer_id,
        "created_customer": created_customer,
    }
