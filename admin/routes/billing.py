"""Billing overview and plan mutations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from admin.auth import CurrentAdmin, SuperAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict

router = APIRouter(prefix="/api/admin/billing", tags=["admin-billing"])


class PlanChange(BaseModel):
    plan: str
    message_limit: int | None = Field(default=None, ge=0)


@router.get("/overview")
async def overview(admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    pool = await get_admin_pool()
    by_plan = await pool.fetch(
        """
        SELECT plan::text AS plan, COUNT(*) AS businesses,
               SUM(messages_used) AS messages_used,
               SUM(message_limit) AS message_limit
        FROM businesses
        WHERE is_active
        GROUP BY plan
        ORDER BY plan
        """
    )
    at_limit = await pool.fetch(
        """
        SELECT id, name, plan, messages_used, message_limit
        FROM businesses
        WHERE is_active AND message_limit > 0
          AND messages_used >= message_limit * 0.9
        ORDER BY messages_used::float / NULLIF(message_limit, 0) DESC
        LIMIT 20
        """
    )
    spend = await pool.fetchval(
        """
        SELECT COALESCE(SUM(estimated_cost_inr), 0)
        FROM usage_events
        WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
        """
    )
    return {
        "plans": [row_dict(r) for r in by_plan],
        "at_limit": [row_dict(r) for r in at_limit],
        "spend_inr_mtd": float(spend or 0),
    }


@router.get("/usage/{business_id}")
async def usage(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    biz = await require_business(business_id)
    pool = await get_admin_pool()
    events = await pool.fetch(
        """
        SELECT event_type, channel::text AS channel,
               SUM(quantity) AS quantity,
               SUM(estimated_cost_inr) AS cost_inr
        FROM usage_events
        WHERE business_id = $1::uuid
          AND created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
        GROUP BY 1, 2
        ORDER BY 1
        """,
        business_id,
    )
    return {
        "business": {
            "id": biz["id"],
            "name": biz["name"],
            "plan": biz.get("plan"),
            "plan_status": biz.get("plan_status"),
            "messages_used": biz.get("messages_used"),
            "message_limit": biz.get("message_limit"),
            "billing_cycle_start": biz.get("billing_cycle_start"),
        },
        "events": [row_dict(r) for r in events],
    }


@router.post("/change-plan/{business_id}")
async def change_plan(business_id: str, body: PlanChange, admin: SuperAdmin) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    if body.message_limit is None:
        row = await pool.fetchrow(
            """
            UPDATE businesses
            SET plan = $1::subscription_plan, updated_at = now()
            WHERE id = $2::uuid
            RETURNING id, name, plan, message_limit
            """,
            body.plan,
            business_id,
        )
    else:
        row = await pool.fetchrow(
            """
            UPDATE businesses
            SET plan = $1::subscription_plan, message_limit = $2, updated_at = now()
            WHERE id = $3::uuid
            RETURNING id, name, plan, message_limit
            """,
            body.plan,
            body.message_limit,
            business_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Business not found")
    await audit(admin.id, "billing.change_plan", business_id=business_id, detail={"plan": body.plan})
    return row_dict(row)


@router.post("/reset-usage/{business_id}")
async def reset_usage(business_id: str, admin: SuperAdmin) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    await pool.execute(
        """
        UPDATE businesses
        SET messages_used = 0, billing_cycle_start = now(), updated_at = now()
        WHERE id = $1::uuid
        """,
        business_id,
    )
    await audit(admin.id, "billing.reset_usage", business_id=business_id)
    return {"id": business_id, "status": "reset"}
