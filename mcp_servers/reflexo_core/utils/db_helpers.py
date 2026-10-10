"""Shared DB helpers for tool handlers."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from shared.db import service_connection, tenant_connection


def vec_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


def row_to_dict(row) -> dict[str, Any]:
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, dict):
            out[k] = v
        else:
            # asyncpg may return JSON as str
            if k in ("data", "preferences", "business_hours", "address", "business_context", "ai_metadata", "trigger_config", "conditions", "action_sequence") and isinstance(v, str):
                try:
                    out[k] = json.loads(v)
                except json.JSONDecodeError:
                    out[k] = v
            else:
                out[k] = v
    return out


async def business_exists(business_id: str) -> bool:
    async with service_connection() as conn:
        val = await conn.fetchval(
            "SELECT 1 FROM businesses WHERE id = $1::uuid AND is_active = true",
            business_id,
        )
        return val is not None


async def get_business_row(business_id: str):
    async with service_connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM businesses WHERE id = $1::uuid",
            business_id,
        )


__all__ = [
    "vec_literal",
    "row_to_dict",
    "business_exists",
    "get_business_row",
    "tenant_connection",
    "service_connection",
]
