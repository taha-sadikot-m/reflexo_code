"""Business channel resolution and Redis event publish helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.db import service_connection
from shared.redis_client import get_redis
from shared.redis_keys import INBOUND_EVENTS_STREAM, channel_account_key

logger = logging.getLogger(__name__)


async def resolve_business_from_channel(channel: str, channel_account_id: str) -> str | None:
    """
    Look up business_id from channel type and account identifier.

    Uses shared channel_account_key so Identity and Meta MCP share the same cache.
    """
    if not channel_account_id:
        return None

    cache_key = channel_account_key(channel, channel_account_id)
    try:
        cached = await get_redis().get(cache_key)
        if cached:
            return cached if isinstance(cached, str) else cached.decode()
    except Exception as exc:
        logger.debug("channel lookup cache error: %s", exc)

    async with service_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT business_id FROM business_channels
            WHERE channel = $1::channel_type
              AND channel_account_id = $2
              AND is_active = true
            LIMIT 1
            """,
            channel,
            channel_account_id,
        )

    if not row:
        return None

    business_id = str(row["business_id"])
    try:
        await get_redis().setex(cache_key, 3600, business_id)
    except Exception:
        pass
    return business_id


async def publish_event(channel: str, event: dict[str, Any]) -> None:
    """Persist an inbound event, then publish a compatibility notification."""
    try:
        payload = json.dumps(event, default=str, separators=(",", ":"))
        redis = get_redis()
        stream_id = await redis.xadd(
            INBOUND_EVENTS_STREAM,
            {"payload": payload},
            maxlen=100_000,
            approximate=True,
        )
        await redis.publish(channel, payload)
        logger.info(
            "Queued event stream_id=%s type=%s channel=%s redis=%s",
            stream_id,
            event.get("event_type"),
            event.get("channel"),
            channel,
        )
    except Exception as exc:
        logger.error("Failed to durably queue event for %s: %s", channel, exc)
        raise
