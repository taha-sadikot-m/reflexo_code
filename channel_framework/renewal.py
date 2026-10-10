"""Webhook subscription renewal."""

from __future__ import annotations

from channel_framework.adapters import get_adapter
from channel_framework.adapters.base import subscription_expiry
from channel_framework.credentials import get_valid_tokens
from shared.db import service_connection


async def renew_expiring_subscriptions() -> dict[str, int]:
    async with service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT business_id::text, platform
            FROM webhook_subscriptions
            WHERE status = 'active'
              AND next_renewal_at <= now()
            """
        )
    renewed = 0
    failed = 0
    for row in rows:
        business_id = row["business_id"]
        platform = row["platform"]
        try:
            credentials = await get_valid_tokens(business_id, platform)
            if not credentials:
                raise RuntimeError("connected account not found")
            subscription = await get_adapter(platform).renew_subscription(
                business_id,
                credentials["access_token"],
            )
            async with service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE webhook_subscriptions
                    SET subscription_id = $3,
                        endpoint_url = $4,
                        expires_at = $5::timestamptz,
                        next_renewal_at = CASE
                            WHEN $5::timestamptz IS NULL THEN NULL
                            ELSE $5::timestamptz - interval '24 hours'
                        END,
                        renewal_failures = 0,
                        last_renewed_at = now(),
                        updated_at = now()
                    WHERE business_id = $1::uuid AND platform = $2
                    """,
                    business_id,
                    platform,
                    subscription.subscription_id,
                    subscription.endpoint_url,
                    subscription_expiry(subscription.expires_at),
                )
            renewed += 1
        except Exception:
            failed += 1
            async with service_connection() as conn:
                await conn.execute(
                    """
                    UPDATE webhook_subscriptions
                    SET renewal_failures = renewal_failures + 1,
                        status = CASE WHEN renewal_failures + 1 >= 3
                            THEN 'renewal_failed' ELSE status END,
                        updated_at = now()
                    WHERE business_id = $1::uuid AND platform = $2
                    """,
                    business_id,
                    platform,
                )
    return {"renewed": renewed, "failed": failed}
