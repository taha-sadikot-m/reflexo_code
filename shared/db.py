"""Async Postgres pool with RLS tenant context helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(
    database_url: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    application_name: str = "reflexo",
) -> asyncpg.Pool:
    """Create the process-wide connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        command_timeout=60,
        server_settings={"application_name": application_name},
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def service_connection() -> AsyncIterator[asyncpg.Connection]:
    """Connection without tenant RLS context (Identity Resolution, admin)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def tenant_connection(business_id: str | UUID) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection and set app.current_business_id for RLS."""
    pool = get_pool()
    bid = str(business_id)
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.current_business_id', $1, false)", bid)
        try:
            yield conn
        finally:
            await conn.execute("SELECT set_config('app.current_business_id', '', false)")
