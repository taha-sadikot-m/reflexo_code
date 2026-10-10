"""Business CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from admin.auth import AdminPlus, CurrentAdmin, SuperAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict
from shared.redis_keys import business_config_key

router = APIRouter(prefix="/api/admin/businesses", tags=["admin-businesses"])


class BusinessCreate(BaseModel):
    name: str
    owner_name: str | None = None
    owner_phone: str
    owner_email: str | None = None
    vertical: str = "general"
    plan: str = "free"
    slug: str | None = None
    persona_name: str | None = None
    greeting_message: str | None = None


class BusinessUpdate(BaseModel):
    name: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    vertical: str | None = None
    is_active: bool | None = None
    persona_name: str | None = None
    persona_tone: str | None = None
    greeting_message: str | None = None
    custom_instructions: str | None = None
    website: str | None = None
    message_limit: int | None = Field(default=None, ge=0)


@router.get("")
async def list_businesses(
    admin: CurrentAdmin,
    q: str | None = None,
    plan: str | None = None,
    status: str | None = Query(default=None, description="active|inactive|onboarding"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del admin
    pool = await get_admin_pool()
    clauses = ["TRUE"]
    params: list[Any] = []
    if q:
        params.append(f"%{q.strip()}%")
        clauses.append(
            f"(name ILIKE ${len(params)} OR owner_name ILIKE ${len(params)} "
            f"OR owner_phone ILIKE ${len(params)} OR slug ILIKE ${len(params)})"
        )
    if plan:
        params.append(plan)
        clauses.append(f"plan::text = ${len(params)}")
    if status == "active":
        clauses.append("is_active = true")
    elif status == "inactive":
        clauses.append("is_active = false")
    elif status == "onboarding":
        clauses.append("onboarding_completed = false")
    where = " AND ".join(clauses)
    total = await pool.fetchval(f"SELECT COUNT(*) FROM businesses WHERE {where}", *params)
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""
        SELECT id, name, slug, owner_name, owner_phone, owner_email, vertical,
               plan, plan_status, message_limit, messages_used, is_active,
               onboarding_completed, created_at, updated_at
        FROM businesses
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return {
        "items": [row_dict(r) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/{business_id}")
async def get_business(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    biz = await require_business(business_id)
    pool = await get_admin_pool()
    channels = await pool.fetch(
        """
        SELECT id, channel, channel_account_id, channel_display_name, is_active,
               last_synced_at, error_message
        FROM business_channels WHERE business_id = $1::uuid
        ORDER BY channel
        """,
        business_id,
    )
    stats = await pool.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE created_at >= now() - interval '7 days') AS messages_7d,
          COUNT(DISTINCT conversation_id) FILTER (WHERE created_at >= now() - interval '7 days') AS convos_7d
        FROM messages WHERE business_id = $1::uuid
        """,
        business_id,
    )
    biz["channels"] = [row_dict(r) for r in channels]
    biz["stats"] = row_dict(stats) if stats else {}
    return biz


@router.post("")
async def create_business(body: BusinessCreate, admin: SuperAdmin) -> dict[str, Any]:
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO businesses (
            name, slug, owner_name, owner_phone, owner_email, vertical, plan, persona_name, greeting_message
        ) VALUES ($1, $2, $3, $4, $5, $6, $7::subscription_plan, $8, $9)
        RETURNING *
        """,
        body.name,
        body.slug,
        body.owner_name,
        body.owner_phone,
        body.owner_email,
        body.vertical,
        body.plan,
        body.persona_name,
        body.greeting_message,
    )
    await audit(admin.id, "business.create", business_id=str(row["id"]))
    return row_dict(row)


@router.patch("/{business_id}")
async def update_business(
    business_id: str, body: BusinessUpdate, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets = []
    params: list[Any] = []
    for key, value in data.items():
        params.append(value)
        sets.append(f"{key} = ${len(params)}")
    params.append(business_id)
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE businesses SET {', '.join(sets)}, updated_at = now()
        WHERE id = ${len(params)}::uuid
        RETURNING *
        """,
        *params,
    )
    try:
        from mcp_servers.reflexo_core.utils.cache import cache_delete

        await cache_delete(business_config_key(business_id))
    except Exception:
        pass
    await audit(admin.id, "business.update", business_id=business_id, detail={"fields": list(data)})
    return row_dict(row)
