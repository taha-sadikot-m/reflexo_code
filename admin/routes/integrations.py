"""Channel Framework integration health."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from admin.auth import AdminPlus, CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import require_business, row_dict

router = APIRouter(prefix="/api/admin/integrations", tags=["admin-integrations"])


@router.get("/health")
async def platform_health(admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT channel::text AS channel,
               COUNT(*) FILTER (WHERE is_active AND error_message IS NULL) AS healthy,
               COUNT(*) FILTER (WHERE error_message IS NOT NULL) AS erroring,
               COUNT(*) AS total
        FROM business_channels
        GROUP BY channel
        ORDER BY channel
        """
    )
    connected = await pool.fetchval(
        "SELECT COUNT(*) FROM connected_accounts WHERE is_active = true"
    )
    return {
        "channels": [row_dict(r) for r in rows],
        "connected_accounts": int(connected or 0),
    }


@router.get("/channels/{business_id}")
async def list_channels(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT id, channel, channel_account_id, channel_display_name, is_active,
               last_synced_at, error_message, token_expires_at, meta_page_data
        FROM business_channels
        WHERE business_id = $1::uuid
        ORDER BY channel
        """,
        business_id,
    )
    return {"items": [row_dict(r) for r in rows]}


@router.post("/channels/{business_id}/test")
async def test_channel(
    business_id: str, admin: AdminPlus, channel_id: str | None = None
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    if channel_id:
        row = await pool.fetchrow(
            """
            SELECT id, channel, is_active, error_message
            FROM business_channels
            WHERE id = $1::uuid AND business_id = $2::uuid
            """,
            channel_id,
            business_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Channel not found")
        ok = bool(row["is_active"]) and not row["error_message"]
        return {"id": str(row["id"]), "ok": ok, "channel": row["channel"]}
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM business_channels WHERE business_id = $1::uuid AND is_active",
        business_id,
    )
    return {"ok": int(count or 0) > 0, "active_channels": int(count or 0)}


@router.get("/accounts/{business_id}")
@router.get("/nango/{business_id}", include_in_schema=False)
async def connected_accounts(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    rows = await pool.fetch(
        """
        SELECT business_id, platform AS integration_id,
               id AS connected_account_id, account_identifier,
               account_display_name, is_active, connection_error,
               updated_at
        FROM connected_accounts
        WHERE business_id = $1::uuid
        """,
        business_id,
    )
    return {"items": [row_dict(r) for r in rows]}
