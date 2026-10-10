"""Onboarding inactivity reminders via Celery Beat."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from onboarding.celery_app import celery_app
from onboarding.whatsapp_send import send_whatsapp_template

logger = logging.getLogger(__name__)


def _run(coro):
    """Run async DB/Graph work from sync Celery tasks."""
    return asyncio.run(coro)


async def _ensure_runtime() -> None:
    import os

    from shared.db import init_pool
    from shared.redis_client import init_redis

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for reminder tasks")
    await init_pool(database_url, application_name="reflexo-celery")
    await init_redis(os.getenv("REDIS_URL", "redis://localhost:6379"))

    encryption_key = (os.getenv("ENCRYPTION_KEY") or "").strip()
    if encryption_key:
        from mcp_servers.meta_mcp.auth.tokens import init_fernet

        init_fernet(encryption_key)


def _hours_since(ts: datetime | None) -> float:
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


async def check_stale_onboarding_async() -> dict[str, Any]:
    """Find incomplete onboarding sessions and send due reminders."""
    await _ensure_runtime()
    from shared.db import service_connection

    async with service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT os.business_id, os.reminder_count, os.last_activity_at,
                   b.owner_phone, b.owner_name,
                   os.progress->>'business_name' AS business_name
            FROM onboarding_sessions os
            JOIN businesses b ON b.id = os.business_id
            WHERE os.is_completed = false
              AND os.is_paused = false
              AND os.last_activity_at < now() - interval '24 hours'
              AND os.reminder_count < 3
            """
        )

    sent = 0
    skipped = 0
    for row in rows:
        result = await send_onboarding_reminder(dict(row))
        if result.get("status") == "sent":
            sent += 1
        else:
            skipped += 1
    return {"checked": len(rows), "sent": sent, "skipped": skipped}


async def send_onboarding_reminder(session: dict[str, Any]) -> dict[str, Any]:
    """Send a reminder based on inactivity interval and prior reminder_count."""
    await _ensure_runtime()
    from shared.db import service_connection

    business_id = str(session["business_id"])
    owner_name = session.get("owner_name") or "there"
    business_name = session.get("business_name") or "your business"
    reminder_count = int(session.get("reminder_count") or 0)
    hours = _hours_since(session.get("last_activity_at"))
    phone = session.get("owner_phone")
    if not phone:
        return {"status": "skipped", "reason": "no_owner_phone"}

    template_name: str | None = None
    params: list[str] = []

    if reminder_count == 0 and hours >= 24:
        template_name = "onboarding_reminder_1"
        params = [owner_name, business_name]
    elif reminder_count == 1 and hours >= 72:
        template_name = "onboarding_reminder_2"
        params = [owner_name]
    elif reminder_count == 2 and hours >= 168:
        template_name = "onboarding_reminder_final"
        params = [owner_name]
    else:
        return {
            "status": "skipped",
            "reason": "interval_not_reached",
            "reminder_count": reminder_count,
            "hours_since_activity": round(hours, 1),
        }

    result = await send_whatsapp_template(
        business_id,
        phone,
        template_name,
        language_code="en",
        template_params=params,
    )

    # Advance count even if Meta rejects (avoid hammering); only pause after 3rd attempt
    async with service_connection() as conn:
        await conn.execute(
            """
            UPDATE onboarding_sessions
            SET reminder_count = reminder_count + 1,
                last_reminder_at = now(),
                is_paused = CASE WHEN reminder_count >= 2 THEN true ELSE false END
            WHERE business_id = $1::uuid AND is_completed = false
            """,
            business_id,
        )

    return {
        "status": result.get("status", "error"),
        "template": template_name,
        "business_id": business_id,
        "template_result": result,
    }


async def cancel_onboarding_reminders(business_id: str) -> dict[str, str]:
    """
    Stop further reminders for a business.

    Query-based Celery checks ignore completed sessions; this marks the session
    completed/paused defensively if completion already flipped businesses row.
    """
    await _ensure_runtime()
    from shared.db import service_connection

    async with service_connection() as conn:
        await conn.execute(
            """
            UPDATE onboarding_sessions
            SET is_paused = true,
                last_activity_at = now()
            WHERE business_id = $1::uuid AND is_completed = false
            """,
            business_id,
        )
    return {"status": "cancelled", "business_id": business_id}


@celery_app.task(name="check_stale_onboarding")
def check_stale_onboarding() -> dict[str, Any]:
    return _run(check_stale_onboarding_async())


@celery_app.task(name="send_onboarding_reminder")
def send_onboarding_reminder_task(session: dict[str, Any]) -> dict[str, Any]:
    return _run(send_onboarding_reminder(session))
