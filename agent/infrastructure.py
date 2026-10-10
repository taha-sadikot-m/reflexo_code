"""Shared checkpointer and long-term memory store singletons."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.base import BaseStore
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent.settings import get_agent_settings

logger = logging.getLogger(__name__)

_store: BaseStore | None = None
_store_pool: AsyncConnectionPool | None = None
_checkpointer_pool: AsyncConnectionPool | None = None
_checkpointer: AsyncPostgresSaver | None = None
_init_lock = asyncio.Lock()

# Neon and other serverless Postgres hosts drop idle connections; keepalives
# reduce mid-request drops while the pool handles checkout recovery.
_CHECKPOINTER_POOL_KWARGS = {
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


def get_store() -> BaseStore:
    """Return the durable customer-memory store (must call init_store first in worker)."""
    global _store
    if _store is None:
        raise RuntimeError("Store not initialized — call await init_store() first")
    return _store


async def init_store() -> BaseStore:
    """Open AsyncPostgresStore (LangGraph) for durable §7.2 customer memory."""
    global _store, _store_pool
    if _store is not None:
        return _store

    async with _init_lock:
        if _store is not None:
            return _store

        settings = get_agent_settings()
        pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=5,
            kwargs=_CHECKPOINTER_POOL_KWARGS,
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        await pool.open()
        store = AsyncPostgresStore(conn=pool)
        await store.setup()
        _store_pool = pool
        _store = store
        logger.info("LangGraph AsyncPostgresStore ready (pooled)")
        return _store


async def ensure_store() -> BaseStore:
    """Store accessor for processes that do not initialize it at startup (API)."""
    if _store is None:
        return await init_store()
    return _store


async def close_store() -> None:
    global _store, _store_pool
    async with _init_lock:
        if _store_pool is not None:
            await _store_pool.close()
            _store_pool = None
            _store = None


async def _open_checkpointer_pool(database_url: str) -> tuple[AsyncConnectionPool, AsyncPostgresSaver]:
    pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=5,
        kwargs=_CHECKPOINTER_POOL_KWARGS,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    return pool, saver


async def init_checkpointer() -> AsyncPostgresSaver:
    """
    Open AsyncPostgresSaver backed by a connection pool.

    Uses psycopg_pool.AsyncConnectionPool so idle Neon connections are
    replaced automatically on the next checkpoint read/write.
    """
    global _checkpointer, _checkpointer_pool
    if _checkpointer is not None:
        return _checkpointer

    async with _init_lock:
        if _checkpointer is not None:
            return _checkpointer

        settings = get_agent_settings()
        _checkpointer_pool, _checkpointer = await _open_checkpointer_pool(
            settings.database_url
        )
        logger.info("LangGraph AsyncPostgresSaver ready (pooled)")
        return _checkpointer


async def get_checkpointer() -> AsyncPostgresSaver:
    if _checkpointer is None:
        return await init_checkpointer()
    return _checkpointer


async def reset_checkpointer() -> AsyncPostgresSaver:
    """Close the pool and rebuild the checkpointer after a stale connection."""
    global _checkpointer, _checkpointer_pool

    async with _init_lock:
        if _checkpointer_pool is not None:
            await _checkpointer_pool.close()
            _checkpointer_pool = None
            _checkpointer = None

        settings = get_agent_settings()
        _checkpointer_pool, _checkpointer = await _open_checkpointer_pool(
            settings.database_url
        )
        logger.info("LangGraph AsyncPostgresSaver reset (pooled)")
        return _checkpointer


async def close_checkpointer() -> None:
    global _checkpointer, _checkpointer_pool

    async with _init_lock:
        if _checkpointer_pool is not None:
            await _checkpointer_pool.close()
            _checkpointer_pool = None
            _checkpointer = None


@asynccontextmanager
async def checkpointer_lifespan() -> AsyncIterator[AsyncPostgresSaver]:
    saver = await init_checkpointer()
    try:
        yield saver
    finally:
        await close_checkpointer()


async def cleanup_old_checkpoints(days: int | None = None) -> int:
    """
    Delete LangGraph checkpoint rows older than `days` (default: AGENT_CONFIG TTL).

    Returns the number of checkpoint rows deleted (best-effort).
    """
    from agent.config import AGENT_CONFIG

    ttl_days = int(days if days is not None else AGENT_CONFIG.get("checkpoint_ttl_days", 30))
    pool = _checkpointer_pool
    if pool is None:
        await init_checkpointer()
        pool = _checkpointer_pool
    if pool is None:
        return 0

    deleted = 0
    async with pool.connection() as conn:
        # LangGraph stores timestamps as text ISO in checkpoint JSON on newer schemas;
        # also prune via checkpoint_writes / blobs by thread orphan after.
        try:
            cur = await conn.execute(
                """
                WITH old AS (
                    SELECT thread_id, checkpoint_ns, checkpoint_id
                    FROM checkpoints
                    WHERE COALESCE(
                        NULLIF(checkpoint->>'ts', '')::timestamptz,
                        NULLIF(metadata->>'created_at', '')::timestamptz,
                        to_timestamp(0)
                    ) < now() - (%s::text || ' days')::interval
                ),
                del_writes AS (
                    DELETE FROM checkpoint_writes w
                    USING old
                    WHERE w.thread_id = old.thread_id
                      AND w.checkpoint_ns = old.checkpoint_ns
                      AND w.checkpoint_id = old.checkpoint_id
                    RETURNING 1
                ),
                del_blobs AS (
                    DELETE FROM checkpoint_blobs b
                    USING old
                    WHERE b.thread_id = old.thread_id
                      AND b.checkpoint_ns = old.checkpoint_ns
                    RETURNING 1
                ),
                del_cp AS (
                    DELETE FROM checkpoints c
                    USING old
                    WHERE c.thread_id = old.thread_id
                      AND c.checkpoint_ns = old.checkpoint_ns
                      AND c.checkpoint_id = old.checkpoint_id
                    RETURNING 1
                )
                SELECT
                    (SELECT COUNT(*) FROM del_cp) AS checkpoints_deleted
                """,
                (str(ttl_days),),
            )
            row = await cur.fetchone()
            if row:
                deleted = int(row["checkpoints_deleted"] if isinstance(row, dict) else row[0] or 0)
        except Exception as exc:
            # Fallback: simple age prune if schema differs
            logger.warning("Structured checkpoint cleanup failed, trying simple delete: %s", exc)
            try:
                cur = await conn.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE created_at < now() - (%s::text || ' days')::interval
                    """,
                    (str(ttl_days),),
                )
                deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except Exception as exc2:
                logger.warning("Checkpoint cleanup unavailable: %s", exc2)
                return 0

    if deleted:
        logger.info("Cleaned up %s old checkpoint row(s) (ttl=%sd)", deleted, ttl_days)
    return deleted


async def clear_thread_checkpoints(thread_id: str) -> int:
    """
    Delete all LangGraph checkpoint rows for one thread_id (last-resort recovery).

    Returns number of checkpoint rows deleted.
    """
    if not thread_id or not str(thread_id).strip():
        return 0

    pool = _checkpointer_pool
    if pool is None:
        await init_checkpointer()
        pool = _checkpointer_pool
    if pool is None:
        return 0

    deleted = 0
    async with pool.connection() as conn:
        try:
            cur = await conn.execute(
                """
                WITH del_writes AS (
                    DELETE FROM checkpoint_writes
                    WHERE thread_id = %(thread_id)s
                    RETURNING 1
                ),
                del_blobs AS (
                    DELETE FROM checkpoint_blobs
                    WHERE thread_id = %(thread_id)s
                    RETURNING 1
                ),
                del_cp AS (
                    DELETE FROM checkpoints
                    WHERE thread_id = %(thread_id)s
                    RETURNING 1
                )
                SELECT (SELECT COUNT(*) FROM del_cp) AS checkpoints_deleted
                """,
                {"thread_id": thread_id},
            )
            row = await cur.fetchone()
            if row:
                deleted = int(row["checkpoints_deleted"] if isinstance(row, dict) else row[0] or 0)
        except Exception as exc:
            logger.warning("clear_thread_checkpoints failed for %s: %s", thread_id, exc)
            return 0

    if deleted:
        logger.warning(
            "Cleared %s checkpoint row(s) for thread_id=%s (orphan history recovery)",
            deleted,
            thread_id,
        )
    return deleted
