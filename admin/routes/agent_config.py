"""Agent persona and config."""

from __future__ import annotations

from typing import Any, Literal
import json

from fastapi import APIRouter
from pydantic import BaseModel

from admin.auth import AdminPlus, CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict
from shared.redis_keys import business_config_key

router = APIRouter(prefix="/api/admin/agent-config", tags=["admin-agent-config"])


class PersonaUpdate(BaseModel):
    persona_name: str | None = None
    persona_tone: str | None = None
    persona_language: list[str] | None = None
    custom_instructions: str | None = None
    greeting_message: str | None = None
    persona: dict[str, Any] | None = None
    smart_number_mode: bool | None = None
    smart_number_default: Literal["ask_owner", "personal", "business"] | None = None


@router.get("/{business_id}")
async def get_config(business_id: str, admin: CurrentAdmin) -> dict[str, Any]:
    del admin
    biz = await require_business(business_id)
    pool = await get_admin_pool()
    entity_count = await pool.fetchval(
        "SELECT COUNT(*) FROM structured_entities WHERE business_id = $1::uuid AND is_active",
        business_id,
    )
    kb_count = await pool.fetchval(
        "SELECT COUNT(*) FROM knowledge_entries WHERE business_id = $1::uuid AND is_active",
        business_id,
    )
    return {
        "persona_name": biz.get("persona_name"),
        "persona_tone": biz.get("persona_tone"),
        "persona_language": biz.get("persona_language"),
        "custom_instructions": biz.get("custom_instructions"),
        "greeting_message": biz.get("greeting_message"),
        "persona": biz.get("persona") or {},
        "smart_number_mode": bool(biz.get("smart_number_mode")),
        "smart_number_default": biz.get("smart_number_default") or "ask_owner",
        "knowledge_entries": int(kb_count or 0),
        "entities": int(entity_count or 0),
        "business": {
            "id": biz["id"],
            "name": biz["name"],
            "vertical": biz.get("vertical"),
        },
    }


@router.put("/{business_id}/persona")
async def update_persona(
    business_id: str, body: PersonaUpdate, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="No fields to update")
    sets = []
    params: list[Any] = []
    for k, v in data.items():
        params.append(v)
        if k == "persona_language":
            sets.append(f"{k} = ${len(params)}::text[]")
        elif k == "persona":
            params[-1] = json.dumps(v)
            sets.append(f"{k} = ${len(params)}::jsonb")
        else:
            sets.append(f"{k} = ${len(params)}")
    params.append(business_id)
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        f"""
        UPDATE businesses SET {', '.join(sets)}, updated_at = now()
        WHERE id = ${len(params)}::uuid
        RETURNING persona_name, persona_tone, persona_language,
                  custom_instructions, greeting_message, persona,
                  smart_number_mode, smart_number_default
        """,
        *params,
    )
    try:
        from mcp_servers.reflexo_core.utils.cache import cache_delete

        await cache_delete(business_config_key(business_id))
    except Exception:
        pass
    await audit(admin.id, "agent_config.update", business_id=business_id)
    return row_dict(row)
