"""Structured entities admin."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from admin.auth import AdminPlus, CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict
from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.tools.entities import VALID_ENTITY_TYPES
from mcp_servers.reflexo_core.utils.db_helpers import vec_literal

router = APIRouter(prefix="/api/admin/entities", tags=["admin-entities"])


class EntityBody(BaseModel):
    entity_type: str
    name: str = Field(min_length=1)
    description: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


@router.get("/{business_id}")
async def list_entities(
    business_id: str,
    admin: CurrentAdmin,
    entity_type: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    clauses = ["business_id = $1::uuid", "is_active = true"]
    params: list[Any] = [business_id]
    if entity_type:
        if entity_type not in VALID_ENTITY_TYPES:
            raise HTTPException(status_code=400, detail="Invalid entity_type")
        params.append(entity_type)
        clauses.append(f"entity_type::text = ${len(params)}")
    if q:
        params.append(f"%{q.strip()}%")
        clauses.append(f"(name ILIKE ${len(params)} OR description ILIKE ${len(params)})")
    where = " AND ".join(clauses)
    total = await pool.fetchval(f"SELECT COUNT(*) FROM structured_entities WHERE {where}", *params)
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""
        SELECT id, entity_type, name, description, data, sort_order, created_at, updated_at
        FROM structured_entities
        WHERE {where}
        ORDER BY sort_order, name
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return {"items": [row_dict(r) for r in rows], "total": int(total or 0)}


@router.post("/{business_id}")
async def create_entity(business_id: str, body: EntityBody, admin: AdminPlus) -> dict[str, Any]:
    await require_business(business_id)
    if body.entity_type not in VALID_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid entity_type")
    vec = vec_literal(await embed(f"{body.name} {body.description or ''}".strip()))
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO structured_entities
            (business_id, entity_type, name, description, data, embedding)
        VALUES ($1::uuid, $2::entity_type, $3, $4, $5::jsonb, $6::vector)
        RETURNING id, entity_type, name, description, data, created_at
        """,
        business_id,
        body.entity_type,
        body.name,
        body.description,
        json.dumps(body.data),
        vec,
    )
    await audit(admin.id, "entity.create", business_id=business_id, entity_id=str(row["id"]))
    return row_dict(row)


@router.put("/{business_id}/{entity_id}")
async def update_entity(
    business_id: str, entity_id: str, body: EntityBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    if body.entity_type not in VALID_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail="Invalid entity_type")
    pool = await get_admin_pool()
    existing = await pool.fetchrow(
        "SELECT id FROM structured_entities WHERE id = $1::uuid AND business_id = $2::uuid",
        entity_id,
        business_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Entity not found")
    vec = vec_literal(await embed(f"{body.name} {body.description or ''}".strip()))
    await pool.execute(
        """
        UPDATE structured_entities
        SET name = $1, description = $2, data = $3::jsonb,
            embedding = $4::vector, entity_type = $5::entity_type, updated_at = now()
        WHERE id = $6::uuid AND business_id = $7::uuid
        """,
        body.name,
        body.description,
        json.dumps(body.data),
        vec,
        body.entity_type,
        entity_id,
        business_id,
    )
    await audit(admin.id, "entity.update", business_id=business_id, entity_id=entity_id)
    return {"id": entity_id, "status": "updated"}


@router.delete("/{business_id}/{entity_id}")
async def delete_entity(business_id: str, entity_id: str, admin: AdminPlus) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    result = await pool.execute(
        """
        UPDATE structured_entities
        SET is_active = false, updated_at = now()
        WHERE id = $1::uuid AND business_id = $2::uuid
        """,
        entity_id,
        business_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Entity not found")
    await audit(admin.id, "entity.delete", business_id=business_id, entity_id=entity_id)
    return {"id": entity_id, "status": "deactivated"}
