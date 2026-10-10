"""Routing analytics over messages.ai_metadata."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from admin.auth import CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import require_business, row_dict

router = APIRouter(prefix="/api/admin/routing", tags=["admin-routing"])


@router.get("/{business_id}/stats")
async def routing_stats(
    business_id: str,
    admin: CurrentAdmin,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    totals = await pool.fetchrow(
        """
        SELECT
          COUNT(*) AS total_messages,
          COUNT(*) FILTER (WHERE role = 'customer') AS customer_messages,
          COUNT(*) FILTER (WHERE role = 'owner') AS owner_messages,
          COUNT(*) FILTER (WHERE role = 'ai') AS ai_messages
        FROM messages
        WHERE business_id = $1::uuid
          AND created_at > now() - ($2 || ' days')::interval
        """,
        business_id,
        str(days),
    )
    specialists = await pool.fetch(
        """
        SELECT
            COALESCE(ai_metadata->>'specialist', 'unknown') AS specialist,
            COUNT(*) AS count,
            AVG(NULLIF(ai_metadata->>'latency_ms','')::float) AS avg_latency_ms
        FROM messages
        WHERE business_id = $1::uuid
          AND role = 'ai'
          AND created_at > now() - ($2 || ' days')::interval
        GROUP BY 1
        ORDER BY count DESC
        """,
        business_id,
        str(days),
    )
    methods = await pool.fetch(
        """
        SELECT
            COALESCE(ai_metadata->>'route_method', 'unknown') AS route_method,
            COUNT(*) AS count
        FROM messages
        WHERE business_id = $1::uuid
          AND role = 'ai'
          AND created_at > now() - ($2 || ' days')::interval
        GROUP BY 1
        """,
        business_id,
        str(days),
    )
    handoff_n = await pool.fetchval(
        """
        SELECT COUNT(*) FROM messages
        WHERE business_id = $1::uuid
          AND role = 'ai'
          AND ai_metadata ? 'handoff_chain'
          AND jsonb_typeof(ai_metadata->'handoff_chain') = 'array'
          AND jsonb_array_length(ai_metadata->'handoff_chain') > 0
          AND created_at > now() - ($2 || ' days')::interval
        """,
        business_id,
        str(days),
    )
    cost = await pool.fetchval(
        """
        SELECT COALESCE(SUM(NULLIF(ai_metadata->>'estimated_cost_usd','')::float), 0)
        FROM messages
        WHERE business_id = $1::uuid
          AND role = 'ai'
          AND created_at > now() - ($2 || ' days')::interval
        """,
        business_id,
        str(days),
    )
    ai_n = int((totals or {}).get("ai_messages") or 0) or 1
    spec_total = sum(int(r["count"]) for r in specialists) or 1
    method_total = sum(int(r["count"]) for r in methods) or 1
    routing = {
        r["specialist"]: {
            "count": int(r["count"]),
            "pct": round(100.0 * int(r["count"]) / spec_total, 1),
            "avg_latency_ms": round(float(r["avg_latency_ms"] or 0), 0),
        }
        for r in specialists
    }
    return {
        "period_days": days,
        "total_messages": int((totals or {}).get("total_messages") or 0),
        "customer_messages": int((totals or {}).get("customer_messages") or 0),
        "owner_messages": int((totals or {}).get("owner_messages") or 0),
        "routing": routing,
        "handoff_rate": round(int(handoff_n or 0) / ai_n, 4),
        "router_methods": {
            r["route_method"]: {
                "count": int(r["count"]),
                "pct": round(100.0 * int(r["count"]) / method_total, 1),
            }
            for r in methods
        },
        "cost_estimate_usd": round(float(cost or 0), 4),
    }


@router.get("/{business_id}/handoffs")
async def handoffs(
    business_id: str,
    admin: CurrentAdmin,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT id, conversation_id, content AS response_text,
               ai_metadata->'handoff_chain' AS chain, created_at
        FROM messages
        WHERE business_id = $1::uuid
          AND role = 'ai'
          AND ai_metadata ? 'handoff_chain'
          AND created_at > now() - ($2 || ' days')::interval
        ORDER BY created_at DESC
        LIMIT 100
        """,
        business_id,
        str(days),
    )
    return {"handoffs": [row_dict(r) for r in rows]}
