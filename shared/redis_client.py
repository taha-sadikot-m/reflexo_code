"""Shared Redis client for Identity Resolution and other services."""

from __future__ import annotations

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
