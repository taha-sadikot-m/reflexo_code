"""Redis-based rate limiter for Meta API calls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp_servers.meta_mcp.redis_keys import meta_rate_key
from mcp_servers.meta_mcp.utils.errors import RateLimitedError
from shared.redis_client import get_redis

RATE_LIMITS: dict[str, dict[str, int]] = {
    "whatsapp_send": {
        "meta_limit": 1000,
        "reflexo_limit": 100,
        "window_seconds": 1,
    },
    "whatsapp_template": {
        "meta_limit": 1000,
        "reflexo_limit": 50,
        "window_seconds": 1,
    },
    "whatsapp_location": {
        "meta_limit": 1000,
        "reflexo_limit": 100,
        "window_seconds": 1,
    },
    "whatsapp_flow": {
        "meta_limit": 1000,
        "reflexo_limit": 100,
        "window_seconds": 1,
    },
    "whatsapp_catalog": {
        "meta_limit": 1000,
        "reflexo_limit": 100,
        "window_seconds": 1,
    },
    "whatsapp_carousel": {
        "meta_limit": 1000,
        "reflexo_limit": 50,
        "window_seconds": 1,
    },
    "instagram_api": {
        "meta_limit": 200,
        "reflexo_limit": 150,
        "window_seconds": 3600,
    },
    "facebook_send": {
        "meta_limit": 200,
        "reflexo_limit": 150,
        "window_seconds": 3600,
    },
}


async def check_rate_limit(business_id: str, endpoint: str) -> None:
    """Raise RateLimitedError if the business exceeded the Reflexo limit for endpoint."""
    config = RATE_LIMITS.get(endpoint)
    if not config:
        return

    window = config["window_seconds"]
    limit = config["reflexo_limit"]
    now = int(datetime.now(timezone.utc).timestamp())
    window_key = now // window
    redis_key = meta_rate_key(business_id, endpoint, window_key)

    client = get_redis()
    current = await client.incr(redis_key)
    if current == 1:
        await client.expire(redis_key, window + 1)

    if current > limit:
        retry_after = window - (now % window)
        raise RateLimitedError(
            endpoint=endpoint,
            limit=limit,
            window_seconds=window,
            retry_after_seconds=max(1, retry_after),
        )


async def get_rate_limit_status(business_id: str, endpoint: str) -> dict[str, Any]:
    config = RATE_LIMITS.get(endpoint, {})
    window = config.get("window_seconds", 1)
    limit = config.get("reflexo_limit", 0)

    now = int(datetime.now(timezone.utc).timestamp())
    window_key = now // window
    redis_key = meta_rate_key(business_id, endpoint, window_key)

    try:
        used = int(await get_redis().get(redis_key) or 0)
    except Exception:
        used = 0

    return {
        "endpoint": endpoint,
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "window_seconds": window,
        "resets_in_seconds": window - (now % window),
    }
