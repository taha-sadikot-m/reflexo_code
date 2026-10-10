"""Bot detection layers + managed numbers sync."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from typing import List, Tuple

from identity.cache import IdentityCache
from identity.models import Channel
from shared.db import get_pool
from shared.redis_keys import MANAGED_NUMBERS_KEY, conv_depth_key, conv_velocity_key

logger = logging.getLogger(__name__)

VELOCITY_WINDOW_SECONDS = 60
VELOCITY_THRESHOLD = 10
DEPTH_THRESHOLD = 200
VELOCITY_KEY_TTL = 300
DEPTH_KEY_TTL = 86400


def _load_thresholds_from_agent_config() -> None:
    """Align bot thresholds with AGENT_CONFIG when the agent package is available."""
    global VELOCITY_WINDOW_SECONDS, VELOCITY_THRESHOLD, DEPTH_THRESHOLD
    try:
        from agent.config import AGENT_CONFIG

        VELOCITY_WINDOW_SECONDS = int(AGENT_CONFIG.get("bot_velocity_window_s", 60))
        VELOCITY_THRESHOLD = int(AGENT_CONFIG.get("bot_velocity_threshold", 10))
        DEPTH_THRESHOLD = int(AGENT_CONFIG.get("bot_depth_threshold", 200))
    except Exception:
        pass


_load_thresholds_from_agent_config()


class BotDetector:
    """Stateless bot detection. Each call checks all layers."""

    def __init__(self) -> None:
        self.cache = IdentityCache()

    async def check(
        self,
        business_id: str,
        sender_external_id: str,
        channel: Channel,
    ) -> Tuple[bool, List[str]]:
        triggered: list[str] = []

        if await self._check_known_number(sender_external_id, channel):
            triggered.append("known_number")

        if await self._check_velocity(business_id, sender_external_id):
            triggered.append("velocity")

        if await self._check_depth(business_id, sender_external_id):
            triggered.append("depth")

        is_suspected = len(triggered) > 0
        if is_suspected:
            logger.warning(
                "Bot suspected: business=%s sender=%s layers=%s",
                business_id,
                sender_external_id,
                triggered,
            )
        return is_suspected, triggered

    async def _check_known_number(self, sender_external_id: str, channel: Channel) -> bool:
        if channel not in (Channel.WHATSAPP, Channel.VOICE, Channel.SMS):
            return False
        return await self.cache.sismember(MANAGED_NUMBERS_KEY, sender_external_id)

    async def _check_velocity(self, business_id: str, sender_external_id: str) -> bool:
        key = conv_velocity_key(business_id, sender_external_id)
        now = time.time()
        member = str(uuid.uuid4())
        await self.cache.zadd(key, {member: now}, ttl=VELOCITY_KEY_TTL)
        await self.cache.zremrangebyscore(key, 0, now - VELOCITY_WINDOW_SECONDS)
        count = await self.cache.zcount(key, now - VELOCITY_WINDOW_SECONDS, now + 1)
        return count > VELOCITY_THRESHOLD

    async def _check_depth(self, business_id: str, sender_external_id: str) -> bool:
        today = date.today().isoformat()
        key = conv_depth_key(business_id, sender_external_id, today)
        try:
            from shared.redis_client import get_redis

            raw = await get_redis().get(key)
            depth = int(raw) if raw else 0
        except Exception as e:
            logger.warning("Redis GET depth failed: %s", e)
            return False
        return depth > DEPTH_THRESHOLD


async def sync_managed_numbers() -> int:
    """Load business_numbers into Redis SET reflexo:managed_numbers."""
    pool = get_pool()
    rows = await pool.fetch(
        "SELECT phone_number FROM business_numbers WHERE is_reflexo_managed = true"
    )
    numbers = [r["phone_number"] for r in rows if r["phone_number"]]
    cache = IdentityCache()
    if numbers:
        # Replace set contents: delete key then sadd
        await cache.delete(MANAGED_NUMBERS_KEY)
        await cache.sadd(MANAGED_NUMBERS_KEY, *numbers)
    logger.info("Synced %d managed numbers to Redis", len(numbers))
    return len(numbers)


async def register_managed_number(
    phone_number: str,
    business_id: str,
    channel: str = "whatsapp",
) -> None:
    """Insert into Postgres and add to Redis SET."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO business_numbers (phone_number, business_id, channel, is_reflexo_managed)
        VALUES ($1, $2::uuid, $3::channel_type, true)
        ON CONFLICT (phone_number) DO UPDATE
        SET business_id = EXCLUDED.business_id,
            channel = EXCLUDED.channel,
            is_reflexo_managed = true
        """,
        phone_number,
        business_id,
        channel,
    )
    cache = IdentityCache()
    await cache.sadd(MANAGED_NUMBERS_KEY, phone_number)


async def increment_conversation_depth(business_id: str, sender_external_id: str) -> int:
    """Called by agent after auto-response. Returns new depth count."""
    today = date.today().isoformat()
    key = conv_depth_key(business_id, sender_external_id, today)
    cache = IdentityCache()
    return await cache.incr(key, ttl=DEPTH_KEY_TTL)
