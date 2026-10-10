"""Onboarding completion flow."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from shared.redis_keys import business_config_key

from mcp_servers.reflexo_core.utils.cache import cache_delete
from mcp_servers.reflexo_core.utils.db_helpers import business_exists, get_business_row, service_connection
from onboarding.progress import load_onboarding_progress, save_onboarding_progress
from onboarding.seeder import seed_vertical_knowledge

logger = logging.getLogger(__name__)


async def complete_onboarding(business_id: str) -> dict[str, Any]:
    """
    Mark onboarding complete, seed vertical knowledge, invalidate config cache.

    Refuses if essential progress fields are still missing.
    """
    if not await business_exists(business_id):
        return {
            "error": {
                "code": "BUSINESS_NOT_FOUND",
                "message": f"No business found with ID {business_id}",
                "agent_guidance": "Check the session business_id.",
            }
        }

    progress = await load_onboarding_progress(business_id)
    if not progress.is_essential_complete():
        remaining = progress.get_required_remaining()
        return {
            "error": {
                "code": "ONBOARDING_INCOMPLETE",
                "message": f"Required fields still missing: {', '.join(remaining)}",
                "agent_guidance": (
                    "Collect the remaining required information (or mark vertical-specific "
                    "fields as skipped) before calling complete_onboarding."
                ),
                "required_remaining": remaining,
            }
        }

    row = await get_business_row(business_id)
    vertical = (row["vertical"] if row else None) or progress.vertical or "general"

    progress.is_completed = True
    progress.completed_at = datetime.now(timezone.utc).isoformat()
    progress.vertical = vertical
    await save_onboarding_progress(business_id, progress)

    async with service_connection() as conn:
        await conn.execute(
            """
            UPDATE businesses
            SET onboarding_completed = true, updated_at = now()
            WHERE id = $1::uuid
            """,
            business_id,
        )
        await conn.execute(
            """
            UPDATE onboarding_sessions
            SET is_completed = true,
                completed_at = now(),
                progress = $2::jsonb,
                last_activity_at = now()
            WHERE business_id = $1::uuid AND is_completed = false
            """,
            business_id,
            progress.to_json(),
        )

    seeded = 0
    try:
        seeded = await seed_vertical_knowledge(business_id, vertical)
    except Exception as exc:
        logger.exception("Vertical seeding failed for %s: %s", business_id, exc)

    await cache_delete(business_config_key(business_id))

    try:
        from onboarding.reminders import cancel_onboarding_reminders

        await cancel_onboarding_reminders(business_id)
    except Exception as exc:
        logger.warning("cancel_onboarding_reminders failed for %s: %s", business_id, exc)

    return {
        "status": "completed",
        "vertical": vertical,
        "knowledge_seeded": seeded,
        "message": (
            "Onboarding complete. The AI employee is now live for customers. "
            "Future owner messages use the normal owner session."
        ),
    }
