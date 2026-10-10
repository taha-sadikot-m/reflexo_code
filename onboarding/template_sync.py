"""Daily WhatsApp template synchronization task."""

from __future__ import annotations

from typing import Any

from onboarding.celery_app import celery_app


async def sync_all_templates_async() -> dict[str, int]:
    from mcp_servers.meta_mcp.tools.templates import (
        sync_whatsapp_templates_impl,
    )
    from onboarding.reminders import _ensure_runtime
    from shared.db import service_connection

    await _ensure_runtime()
    async with service_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT business_id
            FROM business_channels
            WHERE channel = 'whatsapp' AND is_active = true
            """
        )
    synced = 0
    failed = 0
    for row in rows:
        try:
            result: dict[str, Any] = await sync_whatsapp_templates_impl(
                str(row["business_id"])
            )
            synced += int(result.get("synced") or 0)
        except Exception:
            failed += 1
    return {"businesses": len(rows), "templates": synced, "failed": failed}


@celery_app.task(name="sync_whatsapp_templates_daily")
def sync_all_templates_task() -> dict[str, int]:
    from agent.asyncio_compat import run_async

    return run_async(sync_all_templates_async())
