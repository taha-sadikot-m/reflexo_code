"""Shared helpers for admin routes."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from admin.db import get_admin_pool


def encode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, str):
        return value
    return value


def row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    out: dict[str, Any] = {}
    mapping = dict(row)
    json_keys = {
        "data",
        "preferences",
        "business_hours",
        "address",
        "business_context",
        "ai_metadata",
        "trigger_config",
        "conditions",
        "action_sequence",
        "actions_executed",
        "trigger_event",
        "steps",
        "steps_executed",
        "metadata",
        "detail",
    }
    for k, v in mapping.items():
        if k in json_keys and isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        out[k] = encode(v)
    return out


async def require_business(business_id: str) -> dict[str, Any]:
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        "SELECT * FROM businesses WHERE id = $1::uuid",
        business_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Business not found")
    return row_dict(row)


async def audit(
    admin_id: str,
    action: str,
    *,
    business_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        pool = await get_admin_pool()
        await pool.execute(
            """
            INSERT INTO admin_audit_log (admin_id, action, business_id, entity_type, entity_id, detail)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6::jsonb)
            """,
            admin_id,
            action,
            business_id,
            entity_type,
            entity_id,
            json.dumps(detail or {}),
        )
    except Exception:
        return
