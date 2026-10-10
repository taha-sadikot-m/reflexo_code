"""High-priority escalation SMS follow-up (§6.3) — Redis scheduled jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from agent.escalation import SMS_FOLLOWUP_DELAY_SECONDS
from shared.db import get_pool, tenant_connection
from shared.redis_client import get_redis
from shared.redis_keys import ESCALATION_SMS_DUE_KEY, escalation_sms_job_key

logger = logging.getLogger(__name__)

_SMS_BODY = (
    "REFLEXO Alert: A high-priority customer question is waiting for your reply "
    "on WhatsApp. Please respond so we can update the knowledge base."
)


async def schedule_escalation_sms_followup(
    *,
    gap_id: str,
    business_id: str,
    owner_phone: str,
    delay_seconds: int = SMS_FOLLOWUP_DELAY_SECONDS,
) -> None:
    """Enqueue a one-shot SMS if the owner has not responded after `delay_seconds`."""
    if not gap_id or not business_id or not owner_phone:
        return
    due_at = time.time() + max(0, int(delay_seconds))
    payload = {
        "gap_id": gap_id,
        "business_id": business_id,
        "owner_phone": owner_phone,
        "due_at": due_at,
    }
    redis = get_redis()
    job_key = escalation_sms_job_key(gap_id)
    await redis.set(job_key, json.dumps(payload), ex=int(delay_seconds) + 86400)
    await redis.zadd(ESCALATION_SMS_DUE_KEY, {gap_id: due_at})
    logger.info(
        "Scheduled escalation SMS follow-up gap=%s business=%s due_in=%ss",
        gap_id,
        business_id,
        delay_seconds,
    )


async def _gap_still_awaiting_owner(business_id: str, gap_id: str) -> bool:
    async with tenant_connection(business_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM knowledge_gaps
            WHERE id = $1::uuid
              AND business_id = $2::uuid
              AND owner_responded_at IS NULL
              AND sms_followup_sent_at IS NULL
              AND status IN ('open', 'escalated')
            LIMIT 1
            """,
            gap_id,
            business_id,
        )
    return row is not None


async def _mark_sms_sent(business_id: str, gap_id: str) -> None:
    async with tenant_connection(business_id) as conn:
        await conn.execute(
            """
            UPDATE knowledge_gaps
            SET sms_followup_sent_at = now(), updated_at = now()
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            gap_id,
            business_id,
        )


async def _send_owner_sms(business_id: str, owner_phone: str) -> dict[str, Any]:
    """Call Channel MCP send_sms; fall back to its direct provider client."""
    try:
        from agent.tools import call_mcp_tool

        result = await call_mcp_tool(
            "channel",
            "send_sms",
            {
                "business_id": business_id,
                "phone_number": owner_phone,
                "message": _SMS_BODY,
            },
        )
        if isinstance(result, dict):
            return result
        return {"sent": False, "raw": result}
    except Exception as exc:
        logger.warning("send_sms via MCP failed, trying direct client: %s", exc)

    try:
        from channel_framework.integrations.fast2sms import send_sms

        result = await send_sms(
            business_id,
            phone=owner_phone,
            message=_SMS_BODY,
            route="q",
        )
        return {
            "sent": bool(result.get("return", False)),
            "request_id": result.get("request_id", ""),
        }
    except RuntimeError as exc:
        logger.info(
            "SMS follow-up skipped (Fast2SMS not connected) business=%s: %s",
            business_id,
            exc,
        )
        return {"sent": False, "skipped": True, "reason": str(exc)}
    except Exception as exc:
        logger.warning("Direct SMS send failed business=%s: %s", business_id, exc)
        return {"sent": False, "error": str(exc)}


async def process_due_escalation_sms(*, now: float | None = None) -> int:
    """Process due SMS follow-up jobs. Returns number of jobs handled."""
    redis = get_redis()
    ts = now if now is not None else time.time()
    due_ids = await redis.zrangebyscore(ESCALATION_SMS_DUE_KEY, min="-inf", max=ts)
    if not due_ids:
        return 0

    processed = 0
    for raw_id in due_ids:
        gap_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        await redis.zrem(ESCALATION_SMS_DUE_KEY, gap_id)
        job_raw = await redis.get(escalation_sms_job_key(gap_id))
        if not job_raw:
            processed += 1
            continue
        try:
            job = json.loads(job_raw)
        except json.JSONDecodeError:
            processed += 1
            continue

        business_id = str(job.get("business_id") or "")
        owner_phone = str(job.get("owner_phone") or "")
        if not business_id or not owner_phone:
            processed += 1
            continue

        try:
            if not await _gap_still_awaiting_owner(business_id, gap_id):
                logger.info(
                    "SMS follow-up skipped gap=%s (owner responded or already sent)",
                    gap_id,
                )
                await redis.delete(escalation_sms_job_key(gap_id))
                processed += 1
                continue

            result = await _send_owner_sms(business_id, owner_phone)
            if result.get("sent"):
                await _mark_sms_sent(business_id, gap_id)
                logger.info("SMS follow-up sent gap=%s business=%s", gap_id, business_id)
            elif result.get("skipped"):
                # Mark so we do not retry forever when SMS is not connected
                await _mark_sms_sent(business_id, gap_id)
            else:
                logger.warning(
                    "SMS follow-up failed gap=%s result=%s",
                    gap_id,
                    result,
                )
        except Exception as exc:
            logger.exception("SMS follow-up error gap=%s: %s", gap_id, exc)
        finally:
            await redis.delete(escalation_sms_job_key(gap_id))
            processed += 1

    return processed


async def run_escalation_sms_poller(
    stop_event: asyncio.Event,
    *,
    interval_s: float = 60.0,
) -> None:
    """Background loop: process due high-priority escalation SMS jobs."""
    logger.info("Escalation SMS poller started (interval=%ss)", interval_s)
    # Ensure pool is warm (worker already inits it)
    try:
        get_pool()
    except Exception:
        pass
    while not stop_event.is_set():
        try:
            n = await process_due_escalation_sms()
            if n:
                logger.info("Escalation SMS poller processed %s job(s)", n)
        except Exception as exc:
            logger.exception("Escalation SMS poller tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
    logger.info("Escalation SMS poller stopped")
