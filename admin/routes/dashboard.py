"""Platform dashboard aggregates."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from admin.auth import CurrentAdmin
from admin.db import get_admin_pool

router = APIRouter(prefix="/api/admin/dashboard", tags=["admin-dashboard"])


@router.get("")
async def dashboard(
    admin: CurrentAdmin,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    del admin
    pool = await get_admin_pool()
    today = await pool.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC'))
            AS messages_today,
          COUNT(*) FILTER (
            WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
              AND created_at < date_trunc('day', now() AT TIME ZONE 'UTC') + interval '1 day'
              AND date_trunc('day', created_at) = date_trunc('day', now() AT TIME ZONE 'UTC') - interval '1 day'
          ) AS unused
        FROM messages
        """
    )
    yesterday = await pool.fetchval(
        """
        SELECT COUNT(*) FROM messages
        WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') - interval '1 day'
          AND created_at < date_trunc('day', now() AT TIME ZONE 'UTC')
        """
    )
    msgs_today = int((today or {}).get("messages_today") or 0)
    yest = int(yesterday or 0)
    delta_pct = None
    if yest:
        delta_pct = round(((msgs_today - yest) / yest) * 100, 1)

    median_ms = await pool.fetchval(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (
            ORDER BY NULLIF(ai_metadata->>'latency_ms', '')::float
        )
        FROM messages
        WHERE role = 'ai'
          AND created_at >= now() - ($1 || ' days')::interval
          AND (ai_metadata ? 'latency_ms')
        """,
        str(days),
    )
    businesses = await pool.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE is_active) AS active,
          COUNT(*) FILTER (WHERE NOT onboarding_completed AND is_active) AS onboarding
        FROM businesses
        """
    )
    spend = await pool.fetchval(
        """
        SELECT COALESCE(SUM(estimated_cost_inr), 0)
        FROM usage_events
        WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
        """
    )
    series = await pool.fetch(
        """
        SELECT date_trunc('day', created_at)::date AS day, COUNT(*) AS count
        FROM messages
        WHERE created_at >= now() - ($1 || ' days')::interval
        GROUP BY 1
        ORDER BY 1
        """,
        str(days),
    )
    specialists = await pool.fetch(
        """
        SELECT COALESCE(ai_metadata->>'specialist', 'unknown') AS specialist, COUNT(*) AS count
        FROM messages
        WHERE role = 'ai'
          AND created_at >= now() - ($1 || ' days')::interval
        GROUP BY 1
        ORDER BY count DESC
        """,
        str(days),
    )
    total_spec = sum(int(r["count"]) for r in specialists) or 1
    return {
        "messages_today": msgs_today,
        "messages_yesterday": yest,
        "messages_delta_pct": delta_pct,
        "median_reply_ms": round(float(median_ms), 0) if median_ms is not None else None,
        "active_businesses": int((businesses or {}).get("active") or 0),
        "onboarding_businesses": int((businesses or {}).get("onboarding") or 0),
        "spend_inr_today": float(spend or 0),
        "volume": [{"day": str(r["day"]), "count": int(r["count"])} for r in series],
        "specialists": [
            {
                "name": r["specialist"],
                "count": int(r["count"]),
                "share": round(100.0 * int(r["count"]) / total_spec, 1),
            }
            for r in specialists
        ],
    }
