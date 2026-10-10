"""Redis helpers for reflexo-core."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis, from_url

_redis: Redis | None = None


async def init_redis(redis_url: str) -> Redis:
    global _redis
    if _redis is not None:
        return _redis
    _redis = from_url(redis_url, decode_responses=True)
    return _redis


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def cache_get_json(key: str) -> Any | None:
    try:
        raw = await get_redis().get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    try:
        await get_redis().set(key, json.dumps(value, default=str), ex=ttl_seconds)
    except Exception:
        pass


async def cache_delete(key: str) -> None:
    try:
        await get_redis().delete(key)
    except Exception:
        pass


async def cache_delete_prefix(prefix: str) -> None:
    """Delete keys matching prefix* (best-effort)."""
    try:
        client = get_redis()
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=f"{prefix}*", count=100)
            if keys:
                await client.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass
