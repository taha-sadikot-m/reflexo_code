"""Fail-open Redis cache for Identity Resolution."""

from __future__ import annotations

import logging
from typing import Optional

from shared.redis_client import get_redis

logger = logging.getLogger(__name__)


class IdentityCache:
    """Thin wrapper around Redis for identity resolution caching."""

    async def get(self, key: str) -> Optional[str]:
        try:
            return await get_redis().get(key)
        except Exception as e:
            logger.warning("Redis GET failed for key=%s: %s", key, e)
            return None

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        try:
            await get_redis().set(key, value, ex=ttl)
        except Exception as e:
            logger.warning("Redis SET failed for key=%s: %s", key, e)

    async def delete(self, key: str) -> None:
        try:
            await get_redis().delete(key)
        except Exception as e:
            logger.warning("Redis DELETE failed for key=%s: %s", key, e)

    async def delete_pattern(self, pattern: str) -> None:
        try:
            client = get_redis()
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    await client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning("Redis DELETE pattern failed for pattern=%s: %s", pattern, e)

    async def sismember(self, key: str, member: str) -> bool:
        try:
            return bool(await get_redis().sismember(key, member))
        except Exception as e:
            logger.warning("Redis SISMEMBER failed: %s", e)
            return False

    async def sadd(self, key: str, *members: str) -> None:
        try:
            if members:
                await get_redis().sadd(key, *members)
        except Exception as e:
            logger.warning("Redis SADD failed: %s", e)

    async def zadd(self, key: str, mapping: dict, ttl: int | None = None) -> None:
        try:
            client = get_redis()
            await client.zadd(key, mapping)
            if ttl:
                await client.expire(key, ttl)
        except Exception as e:
            logger.warning("Redis ZADD failed: %s", e)

    async def zcount(self, key: str, min_score: float, max_score: float) -> int:
        try:
            return int(await get_redis().zcount(key, min_score, max_score))
        except Exception as e:
            logger.warning("Redis ZCOUNT failed: %s", e)
            return 0

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        try:
            await get_redis().zremrangebyscore(key, min_score, max_score)
        except Exception as e:
            logger.warning("Redis ZREMRANGEBYSCORE failed: %s", e)

    async def incr(self, key: str, ttl: int | None = None) -> int:
        try:
            client = get_redis()
            val = await client.incr(key)
            if ttl and val == 1:
                await client.expire(key, ttl)
            return int(val)
        except Exception as e:
            logger.warning("Redis INCR failed: %s", e)
            return 0
