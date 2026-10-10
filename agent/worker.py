"""Agent worker process entrypoint.

Usage:
    python -m agent.worker
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

# Settings read .env via pydantic-settings, which does not export into
# os.environ — load it here too so os.getenv consumers (e.g. embeddings) match.
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from agent.asyncio_compat import run_async
from agent.error_handling import run_retry_poller
from agent.infrastructure import (
    cleanup_old_checkpoints,
    close_checkpointer,
    close_store,
    init_checkpointer,
    init_store,
)
from agent.logging_setup import configure_worker_logging
from agent.redis_bus import run_incoming_subscriber
from agent.session import handle_redis_event
from agent.settings import get_agent_settings
from agent.sms_followup import run_escalation_sms_poller
from agent.tools import (
    close_mcp_sessions,
    ensure_server_session,
    format_exception,
    init_mcp_sessions,
    mcp_tool_counts,
    missing_mcp_servers,
)
from shared.db import close_pool, init_pool
from shared.redis_client import close_redis, get_redis, init_redis

logger = logging.getLogger(__name__)


async def _async_main() -> None:
    settings = get_agent_settings()
    worker_log_path = configure_worker_logging(
        level=settings.log_level,
        log_file=settings.agent_log_file,
        max_bytes=settings.agent_log_max_bytes,
        backup_count=settings.agent_log_backup_count,
        root=ROOT,
    )

    if not settings.google_api_key:
        logger.error("GOOGLE_API_KEY is required for the agent worker")
        sys.exit(1)

    await init_pool(
        settings.database_url,
        max_size=settings.database_pool_size,
        application_name="reflexo-agent",
    )
    await init_redis(settings.redis_url)
    await init_checkpointer()
    await init_store()

    try:
        n = await cleanup_old_checkpoints()
        if n:
            logger.info("Startup checkpoint cleanup removed %s row(s)", n)
    except Exception as exc:
        logger.warning("Startup checkpoint cleanup skipped: %s", exc)

    try:
        from identity.bot_detection import sync_managed_numbers

        count = await sync_managed_numbers()
        logger.info("Synced %d managed numbers for bot detection", count)
    except Exception as exc:
        logger.warning("sync_managed_numbers skipped: %s", exc)

    try:
        await init_mcp_sessions()
    except Exception as exc:
        logger.warning(
            "Could not load MCP catalogs (Core/Meta may be down) — "
            "will retry on first use:\n%s",
            format_exception(exc),
        )

    # Core often starts a few seconds after the worker — retry missing peers only
    for attempt in range(1, 4):
        missing = missing_mcp_servers()
        if not missing:
            break
        logger.warning(
            "MCP catalogs still missing %s — retry %s/3 in 2s",
            missing,
            attempt,
        )
        await asyncio.sleep(2.0)
        for server_name in missing:
            ok = await ensure_server_session(server_name)
            if ok:
                logger.info("Recovered MCP catalog '%s' on startup retry", server_name)
    still_missing = missing_mcp_servers()
    if still_missing:
        logger.warning(
            "Starting with incomplete MCP catalogs (missing=%s) — "
            "will ensure on each message",
            still_missing,
        )

    redis_ready = False
    try:
        redis_ready = bool(await get_redis().ping())
    except Exception as exc:
        logger.warning("Redis readiness check failed: %s", type(exc).__name__)
    counts = mcp_tool_counts()
    logger.info(
        "READINESS redis=%s core_mcp=%s core_tools=%d meta_mcp=%s meta_tools=%d log_file=%s",
        "ready" if redis_ready else "unavailable",
        "ready" if "core" not in still_missing else "unavailable",
        counts["core"],
        "ready" if "meta" not in still_missing else "unavailable",
        counts["meta"],
        worker_log_path or "console-only",
    )

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda *_: _request_stop())

    logger.info(
        "Agent worker starting (core=%s meta=%s)",
        settings.core_mcp_url,
        settings.meta_mcp_tools_url,
    )

    async def _handler(event: dict) -> None:
        logger.info(
            "EVENT_PROCESS_START channel=%s event_type=%s business=%s",
            event.get("channel"),
            event.get("event_type"),
            event.get("business_id"),
        )
        result = await handle_redis_event(event)
        if result:
            logger.info(
                "EVENT_PROCESS_END event_type=%s status=%s outbound=%s latency_ms=%s",
                event.get("event_type"),
                result.get("status"),
                result.get("outbound"),
                result.get("latency_ms"),
            )
        else:
            logger.info(
                "EVENT_PROCESS_END event_type=%s status=dropped",
                event.get("event_type"),
            )

    async def _retry_handler(event: dict):
        return await handle_redis_event(event)

    retry_task = asyncio.create_task(run_retry_poller(_retry_handler, stop_event))
    sms_task = asyncio.create_task(run_escalation_sms_poller(stop_event))
    cleanup_task = asyncio.create_task(_run_daily_checkpoint_cleanup(stop_event))

    try:
        await run_incoming_subscriber(_handler, stop_event=stop_event)
    finally:
        stop_event.set()
        for task in (retry_task, sms_task, cleanup_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.shield(close_mcp_sessions())
        except BaseException as exc:
            logger.warning("MCP session shutdown: %s", exc)
        await close_store()
        await close_checkpointer()
        await close_redis()
        await close_pool()
        logger.info("Agent worker stopped")


async def _run_daily_checkpoint_cleanup(
    stop_event: asyncio.Event,
    *,
    interval_s: float = 86400.0,
) -> None:
    """Periodic checkpoint TTL cleanup (spec §7.1 daily cron)."""
    logger.info("Checkpoint cleanup loop started (interval=%ss)", interval_s)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            break
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            n = await cleanup_old_checkpoints()
            if n:
                logger.info("Daily checkpoint cleanup removed %s row(s)", n)
        except Exception as exc:
            logger.warning("Daily checkpoint cleanup failed: %s", exc)
    logger.info("Checkpoint cleanup loop stopped")


def main() -> None:
    run_async(_async_main())


if __name__ == "__main__":
    main()
