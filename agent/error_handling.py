"""Error recovery helpers for tool and LLM failures."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agent.config import AGENT_CONFIG
from shared.redis_client import get_redis
from shared.redis_keys import RETRY_QUEUE_INDEX_KEY, retry_queue_key

logger = logging.getLogger(__name__)

# Internal-facing: names the real fault, for logs and owner notifications.
# Never send this to a customer -- our outages are not their concern, and it
# used to be sent on every timeout, once per retry.
LLM_FALLBACK_MESSAGE = (
    "I am experiencing a temporary issue. "
    "I have noted your message and will get back to you shortly."
)

# Customer-facing last resort, only after retries are exhausted. Reads as
# ordinary service rather than an incident report.
HOLDING_MESSAGE = "Let me check on that and get back to you shortly."

# A slow turn retried five times spends minutes of latency on a customer who
# has already moved on.
RETRY_MAX_ATTEMPTS = 2

RECURSION_FALLBACK_MESSAGE = (
    "I was not able to fully process your request. "
    "Let me connect you with the team for help."
)

LIMIT_REACHED_MESSAGE = (
    "Your AI assistant has reached its message limit for this month. "
    "Please ask the business owner to upgrade their plan to continue."
)

OWNER_LIMIT_NOTIFICATION = (
    "Hi {owner_name}, your REFLEXO AI has reached its message limit "
    "({limit} messages) for this billing cycle. "
    "Customers will see a limit message until the cycle resets or you upgrade. "
    "Reply 'upgrade' to see plan options."
)


class ToolCallRetryWrapper:
    """Retry a tool call once, then return a structured error for the LLM."""

    @staticmethod
    async def execute_with_retry(tool_call_fn: Callable[[], Awaitable[Any]]) -> Any:
        max_retries = int(AGENT_CONFIG["tool_retry_max"])
        delay = float(AGENT_CONFIG["tool_retry_delay_s"])
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                return await tool_call_fn()
            except Exception as exc:  # noqa: BLE001 — surface to LLM as guidance
                from langgraph.errors import GraphBubbleUp

                if isinstance(exc, GraphBubbleUp):
                    raise
                last_error = exc
                logger.warning("Tool call failed (attempt %s): %s", attempt + 1, exc)
                if attempt < max_retries:
                    await asyncio.sleep(delay)

        return {
            "error": {
                "code": "TOOL_CALL_FAILED",
                "message": f"Tool call failed after retry: {last_error}",
                "agent_guidance": (
                    "This tool is temporarily unavailable. "
                    "Inform the customer that you are experiencing a temporary issue "
                    "and ask them to try again in a few minutes. "
                    "Do NOT retry this tool again in this turn."
                ),
            }
        }


async def queue_for_retry(
    business_id: str,
    customer_id: str | None,
    channel: str,
    message_text: str,
    error: str,
    *,
    external_sender_id: str | None = None,
    receiving_account_id: str | None = None,
    message_id: str | None = None,
    comment_id: str | None = None,
    skip_send: bool = False,
    simulator: dict[str, Any] | None = None,
) -> None:
    """Push a failed turn onto Redis for the retry poller.

    `message_id` carries the original inbound id so the replay is bound to the
    same answered-marker and cannot produce a second customer reply.

    `skip_send`/`simulator` are persisted because a replay that loses them would
    run as live traffic: identity resolution fails on a simulator account and the
    exhausted chain then sends a real holding message.
    """
    entry = json.dumps(
        {
            "business_id": business_id,
            "customer_id": customer_id,
            "channel": channel,
            "message_text": message_text,
            "error": error,
            "external_sender_id": external_sender_id,
            "receiving_account_id": receiving_account_id,
            "message_id": message_id,
            "comment_id": comment_id,
            "skip_send": bool(skip_send),
            "simulator": simulator or None,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
        }
    )
    key = retry_queue_key(business_id)
    try:
        redis = get_redis()
        await redis.rpush(key, entry)
        await redis.expire(key, 86400)
        await redis.sadd(RETRY_QUEUE_INDEX_KEY, business_id)
    except Exception as exc:
        logger.error("Failed to queue retry for %s: %s", business_id, exc)


async def notify_owner_of_outage(
    business_id: str,
    error: str,
    *,
    skip_send: bool = False,
) -> None:
    """WhatsApp the owner when the agent/LLM path fails (§11.2)."""
    if skip_send:
        logger.info("Owner outage notice skipped (skip_send) business=%s", business_id)
        return
    try:
        from mcp_servers.reflexo_core.utils.db_helpers import get_business_row
        from agent.tools import call_mcp_tool

        biz = await get_business_row(business_id)
        if not biz:
            logger.warning("Cannot notify owner of outage — business not found: %s", business_id)
            return
        owner_phone = biz["owner_phone"]
        owner_name = biz["owner_name"] or "there"
        if not owner_phone:
            logger.warning("Cannot notify owner of outage — no owner_phone for %s", business_id)
            return
        text = (
            f"Hi {owner_name}, REFLEXO hit a temporary issue processing a customer "
            f"message. I queued it for retry.\n"
            f"Error: {str(error)[:300]}"
        )
        await call_mcp_tool(
            "meta",
            "send_whatsapp_message",
            {"business_id": business_id, "to": owner_phone, "text": text},
        )
    except Exception as exc:
        logger.warning("notify_owner_of_outage failed: %s", exc)


async def notify_owner_of_limit(business_id: str) -> None:
    """Notify the owner that the billing-cycle message limit was hit (§11.3)."""
    try:
        from mcp_servers.reflexo_core.utils.db_helpers import get_business_row
        from agent.tools import call_mcp_tool
        from agent.pipeline import load_business_config

        biz = await get_business_row(business_id)
        if not biz:
            logger.warning("Cannot notify owner of limit — business not found: %s", business_id)
            return
        config = await load_business_config(business_id)
        owner_phone = biz["owner_phone"]
        if not owner_phone:
            logger.warning("Cannot notify owner of limit — no owner_phone for %s", business_id)
            return
        text = OWNER_LIMIT_NOTIFICATION.format(
            owner_name=biz["owner_name"] or "there",
            limit=int(config.get("message_limit") or 100),
        )
        await call_mcp_tool(
            "meta",
            "send_whatsapp_message",
            {"business_id": business_id, "to": owner_phone, "text": text},
        )
    except Exception as exc:
        logger.warning("notify_owner_of_limit failed: %s", exc)


async def process_retry_queues(
    handler: Callable[[dict[str, Any]], Awaitable[Any]],
    *,
    max_per_tick: int = 20,
) -> int:
    """
    Drain a slice of retry queues and re-invoke the session handler.

    Returns the number of entries attempted.
    """
    try:
        redis = get_redis()
    except Exception as exc:
        logger.debug("retry poller: redis unavailable: %s", exc)
        return 0

    try:
        business_ids = list(await redis.smembers(RETRY_QUEUE_INDEX_KEY) or set())
    except Exception as exc:
        logger.warning("retry poller: cannot read index: %s", exc)
        return 0

    processed = 0
    for raw_bid in business_ids:
        if processed >= max_per_tick:
            break
        business_id = raw_bid.decode() if isinstance(raw_bid, bytes) else str(raw_bid)
        key = retry_queue_key(business_id)
        # Requeues are held back until the tick ends. Pushing them straight back
        # onto the same key means the loop below pops them again immediately, so
        # a turn failing on a slow dependency burns every attempt in one pass
        # and reaches the holding message within seconds.
        deferred: list[dict[str, Any]] = []
        while processed < max_per_tick:
            raw = await redis.lpop(key)
            if raw is None:
                await redis.srem(RETRY_QUEUE_INDEX_KEY, business_id)
                break
            try:
                entry = json.loads(raw if isinstance(raw, str) else raw.decode())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("retry poller: bad entry: %s", exc)
                processed += 1
                continue

            sender = entry.get("external_sender_id")
            receiving = entry.get("receiving_account_id")
            channel = entry.get("channel")
            text = entry.get("message_text")
            if not (sender and receiving and channel and text):
                logger.warning("retry poller: incomplete entry for %s — dropping", business_id)
                processed += 1
                continue

            replay: dict[str, Any] = {
                "event_type": "retry",
                "channel": channel,
                "sender_id": sender,
                "phone_number_id": receiving,
                "receiving_account_id": receiving,
                "content": {"text": text},
                "message_type": "text",
                "business_id": business_id,
                "customer_id": entry.get("customer_id"),
                "message_id": entry.get("message_id"),
                "comment_id": entry.get("comment_id"),
            }
            if entry.get("skip_send"):
                replay["skip_send"] = True
            if isinstance(entry.get("simulator"), dict):
                replay["simulator"] = entry["simulator"]

            try:
                result = await handler(replay)
                status = (result or {}).get("status") if isinstance(result, dict) else None
                if status == "error":
                    await _requeue_or_give_up(entry, deferred)
            except Exception as exc:
                logger.exception("retry poller handler failed: %s", exc)
                entry["error"] = str(exc)
                await _requeue_or_give_up(entry, deferred)
            processed += 1

        for pending in deferred:
            await redis.rpush(key, json.dumps(pending))
            await redis.expire(key, 86400)
            await redis.sadd(RETRY_QUEUE_INDEX_KEY, business_id)

    return processed


async def _requeue_or_give_up(entry: dict[str, Any], deferred: list[dict[str, Any]]) -> None:
    """Hold a failed retry for the next tick, or hold the customer at the cap."""
    attempts = int(entry.get("attempts") or 0) + 1
    if attempts < RETRY_MAX_ATTEMPTS:
        entry["attempts"] = attempts
        deferred.append(entry)
        return

    business_id = entry.get("business_id") or ""
    logger.error(
        "retry poller: giving up on message for business=%s after %s attempts: %s",
        business_id,
        attempts,
        entry.get("error"),
    )
    await send_holding_message(entry)


async def send_holding_message(entry: dict[str, Any]) -> None:
    """Last resort after a retry chain is exhausted — one neutral reply.

    Skipped when the answered marker shows the customer already heard back, so
    an earlier successful reply is never followed by a holding message.
    """
    channel = entry.get("channel")
    recipient = entry.get("external_sender_id")
    if not (channel and recipient):
        return
    if entry.get("skip_send") or entry.get("simulator"):
        logger.info("Holding message skipped — simulator entry channel=%s", channel)
        return
    try:
        from agent.pipeline import (
            mark_message_answered,
            message_already_answered,
            send_response,
        )

        message_id = entry.get("message_id")
        if await message_already_answered(str(channel), message_id):
            logger.info("Holding message skipped — already answered id=%s", message_id)
            return
        await send_response(
            channel=str(channel),
            recipient_id=str(recipient),
            text=HOLDING_MESSAGE,
            business_id=str(entry.get("business_id") or ""),
            comment_id=entry.get("comment_id"),
        )
        await mark_message_answered(str(channel), message_id)
    except Exception as exc:
        logger.warning("Holding message send failed: %s", exc)


async def run_retry_poller(
    handler: Callable[[dict[str, Any]], Awaitable[Any]],
    stop_event: asyncio.Event,
    *,
    interval_s: float = 30.0,
) -> None:
    """Background loop: poll retry queues every `interval_s` until stop_event."""
    logger.info("Retry poller started (interval=%ss)", interval_s)
    while not stop_event.is_set():
        try:
            n = await process_retry_queues(handler)
            if n:
                logger.info("Retry poller processed %s queued message(s)", n)
        except Exception as exc:
            logger.exception("Retry poller tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("Retry poller stopped")
