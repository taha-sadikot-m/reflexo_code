"""Durable Redis Stream consumer for Meta inbound events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
from typing import Any, Awaitable, Callable
from uuid import uuid4

from redis.exceptions import ResponseError
from agent.logging_setup import correlation_context
from shared.redis_client import get_redis
from shared.redis_keys import INBOUND_EVENTS_GROUP, INBOUND_EVENTS_STREAM

logger = logging.getLogger(__name__)

IncomingHandler = Callable[[dict[str, Any]], Awaitable[Any]]

# Bound parallel inbound turns across businesses; one conversation stays serial.
_MAX_INBOUND_CONCURRENCY = 8


def _conversation_key(event: dict[str, Any]) -> str:
    return (
        f"{event.get('business_id') or ''}:"
        f"{event.get('sender_id') or ''}:"
        f"{event.get('channel') or ''}"
    )


def _correlation_for(stream_id: str, event: dict[str, Any]) -> str:
    source = (
        f"{event.get('channel') or ''}:"
        f"{event.get('message_id') or event.get('comment_id') or stream_id}"
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


async def run_incoming_subscriber(
    handler: IncomingHandler,
    *,
    pattern: str = "incoming:*",
    stop_event: asyncio.Event | None = None,
    max_concurrency: int = _MAX_INBOUND_CONCURRENCY,
    claim_idle_ms: int = 60_000,
) -> None:
    """
    Consume inbound events from a Redis consumer group.

    Acknowledge only after a successful handler return. Stale pending entries
    are claimed on startup; per-conversation locks preserve checkpoint safety.
    ``pattern`` remains accepted for compatibility but Pub/Sub is not consumed.
    """
    redis = get_redis()
    stop = stop_event or asyncio.Event()
    consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    try:
        await redis.xgroup_create(
            INBOUND_EVENTS_STREAM,
            INBOUND_EVENTS_GROUP,
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
    locks: dict[str, asyncio.Lock] = {}
    locks_guard = asyncio.Lock()
    pending: set[asyncio.Task[Any]] = set()

    async def _lock_for(key: str) -> asyncio.Lock:
        async with locks_guard:
            lock = locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                locks[key] = lock
            return lock

    async def _run_handler(stream_id: str, event: dict[str, Any]) -> None:
        correlation_id = _correlation_for(stream_id, event)
        with correlation_context(correlation_id):
            async with semaphore:
                lock = await _lock_for(_conversation_key(event))
                async with lock:
                    logger.info(
                        "EVENT_DISPATCH stream_id=%s channel=%s event_type=%s business=%s",
                        stream_id,
                        event.get("channel"),
                        event.get("event_type"),
                        event.get("business_id"),
                    )
                    try:
                        await handler(event)
                    except Exception:
                        logger.exception(
                            "EVENT_FAILED stream_id=%s event_type=%s business=%s; "
                            "left pending for recovery",
                            stream_id,
                            event.get("event_type"),
                            event.get("business_id"),
                        )
                        return
                    await redis.xack(
                        INBOUND_EVENTS_STREAM,
                        INBOUND_EVENTS_GROUP,
                        stream_id,
                    )
                    logger.info("EVENT_ACK stream_id=%s", stream_id)

    def _schedule(stream_id: str, fields: dict[str, Any]) -> None:
        raw = fields.get("payload") or fields.get(b"payload")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.error("Invalid inbound stream payload stream_id=%s", stream_id)
            return
        if not isinstance(event, dict):
            logger.error("Non-object inbound stream payload stream_id=%s", stream_id)
            return
        logger.info(
            "EVENT_RECEIVED corr=%s stream_id=%s channel=%s event_type=%s business=%s",
            _correlation_for(stream_id, event),
            stream_id,
            event.get("channel"),
            event.get("event_type"),
            event.get("business_id"),
        )
        task = asyncio.create_task(
            _run_handler(stream_id, event),
            name=f"inbound:{event.get('message_id') or event.get('event_type')}",
        )
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def _recover_stale() -> int:
        recovered = 0
        start_id = "0-0"
        while not stop.is_set():
            result = await redis.xautoclaim(
                INBOUND_EVENTS_STREAM,
                INBOUND_EVENTS_GROUP,
                consumer,
                claim_idle_ms,
                start_id=start_id,
                count=max_concurrency,
            )
            start_id, messages = result[0], result[1]
            for stream_id, fields in messages:
                _schedule(stream_id, fields)
                recovered += 1
            if not messages or start_id == "0-0":
                break
            await asyncio.sleep(0)
        return recovered

    recovered = await _recover_stale()
    logger.info(
        "Consuming Redis stream=%s group=%s consumer=%s recovered=%d",
        INBOUND_EVENTS_STREAM,
        INBOUND_EVENTS_GROUP,
        consumer,
        recovered,
    )
    try:
        while not stop.is_set():
            batches = await redis.xreadgroup(
                INBOUND_EVENTS_GROUP,
                consumer,
                {INBOUND_EVENTS_STREAM: ">"},
                count=max_concurrency,
                block=1_000,
            )
            for _stream, messages in batches:
                for stream_id, fields in messages:
                    _schedule(stream_id, fields)
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
