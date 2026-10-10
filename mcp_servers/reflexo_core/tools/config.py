"""Business configuration tools."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from shared.redis_keys import business_config_key

from mcp_servers.reflexo_core.utils.cache import cache_delete, cache_get_json, cache_set_json
from mcp_servers.reflexo_core.utils.db_helpers import business_exists, get_business_row, service_connection
from mcp_servers.reflexo_core.utils.errors import tool_error

ALLOWED_FIELDS = {
    "name",
    "description",
    "vertical",
    "owner_name",
    "business_hours",
    "address",
    "website",
    "persona_name",
    "persona_tone",
    "persona_language",
    "custom_instructions",
    "greeting_message",
    "business_context",
    "smart_number_mode",
    "smart_number_default",
    "persona",
}

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_CONTEXT_SEGMENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_CONTEXT_DEPTH = 6


def parse_context_path(path: str) -> list[str] | None:
    """Split contact.email / contact/email into snake_case segments."""
    raw = (path or "").strip().replace("/", ".")
    parts = [p.strip().lower() for p in raw.split(".") if p.strip()]
    if not parts or len(parts) > _MAX_CONTEXT_DEPTH:
        return None
    if not all(_CONTEXT_SEGMENT.match(p) for p in parts):
        return None
    return parts


def merge_context_path(
    context: dict[str, Any] | None,
    parts: list[str],
    value: Any,
) -> tuple[Any, dict[str, Any]]:
    """Deep-merge value at path; return (previous_leaf, new_context)."""
    ctx: dict[str, Any] = copy.deepcopy(context) if isinstance(context, dict) else {}
    cursor: Any = ctx
    for key in parts[:-1]:
        nxt = cursor.get(key) if isinstance(cursor, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            if isinstance(cursor, dict):
                cursor[key] = nxt
        cursor = nxt
    leaf = parts[-1]
    if not isinstance(cursor, dict):
        cursor = ctx
    old_value = cursor.get(leaf)
    cursor[leaf] = value
    return old_value, ctx


def _parse_json_field(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    return val


async def get_business_config_impl(business_id: str) -> dict[str, Any] | None:
    key = business_config_key(business_id)
    cached = await cache_get_json(key)
    if cached is not None:
        return cached

    row = await get_business_row(business_id)
    if not row:
        return None

    async with service_connection() as conn:
        channels = await conn.fetch(
            """
            SELECT channel, channel_display_name, is_active
            FROM business_channels
            WHERE business_id = $1::uuid
            """,
            business_id,
        )

    config = {
        "id": str(row["id"]),
        "name": row["name"],
        "vertical": row["vertical"],
        "owner_name": row["owner_name"],
        "owner_phone": row["owner_phone"],
        "business_hours": _parse_json_field(row["business_hours"]) or {},
        "address": _parse_json_field(row["address"]) or {},
        "business_context": _parse_json_field(row["business_context"]) or {},
        "website": row["website"],
        "persona_name": row["persona_name"],
        "persona_tone": row["persona_tone"],
        "persona_language": list(row["persona_language"] or []),
        "custom_instructions": row["custom_instructions"],
        "greeting_message": row["greeting_message"],
        "plan": row["plan"],
        "messages_used": row["messages_used"],
        "message_limit": row["message_limit"],
        "billing_cycle_start": row["billing_cycle_start"].isoformat()
        if row.get("billing_cycle_start")
        else None,
        "onboarding_completed": row["onboarding_completed"],
        "smart_number_mode": bool(row.get("smart_number_mode")),
        "smart_number_default": row.get("smart_number_default") or "ask_owner",
        "persona": _parse_json_field(row.get("persona")) or {},
        "connected_channels": [
            {
                "channel": c["channel"],
                "display_name": c["channel_display_name"],
                "is_active": c["is_active"],
            }
            for c in channels
        ],
    }
    await cache_set_json(key, config, 300)
    return config


async def upsert_business_context_impl(
    business_id: str,
    path: str,
    value: Any,
) -> dict[str, Any]:
    """Merge one fact into businesses.business_context without wiping siblings."""
    parts = parse_context_path(path)
    if parts is None:
        return tool_error(
            "INVALID_INPUT",
            "path must be dotted snake_case like contact.email or extra.parking",
        )
    if not await business_exists(business_id):
        return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

    row = await get_business_row(business_id)
    current = _parse_json_field(row["business_context"]) if row else {}
    old_value, new_ctx = merge_context_path(
        current if isinstance(current, dict) else {},
        parts,
        value,
    )

    async with service_connection() as conn:
        await conn.execute(
            """
            UPDATE businesses
            SET business_context = $1::jsonb, updated_at = now()
            WHERE id = $2::uuid
            """,
            json.dumps(new_ctx),
            business_id,
        )
    await cache_delete(business_config_key(business_id))
    return {
        "path": ".".join(parts),
        "old_value": old_value,
        "new_value": value,
        "context": new_ctx,
        "status": "updated",
    }


async def update_business_info_impl(
    business_id: str,
    field: str,
    value: Any,
) -> dict[str, Any]:
    """Update one validated business field for MCP and trusted local agents."""
    if field not in ALLOWED_FIELDS:
        return tool_error("FIELD_NOT_ALLOWED", f"Cannot update field '{field}'")
    if not await business_exists(business_id):
        return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

    row = await get_business_row(business_id)
    old_value = _parse_json_field(row[field]) if row else None

    if field in ("business_hours", "address", "business_context", "persona") and isinstance(value, (dict, list)):
        db_value = json.dumps(value)
        sql = f"UPDATE businesses SET {field} = $1::jsonb, updated_at = now() WHERE id = $2::uuid"
    elif field == "persona_language" and isinstance(value, list):
        db_value = value
        sql = "UPDATE businesses SET persona_language = $1::text[], updated_at = now() WHERE id = $2::uuid"
    else:
        db_value = value if not isinstance(value, (dict, list)) else json.dumps(value)
        sql = f"UPDATE businesses SET {field} = $1, updated_at = now() WHERE id = $2::uuid"

    async with service_connection() as conn:
        await conn.execute(sql, db_value, business_id)

    await cache_delete(business_config_key(business_id))
    return {
        "field": field,
        "old_value": old_value,
        "new_value": value,
        "status": "updated",
    }


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_business_config(business_id: str) -> dict[str, Any]:
        """Get the full business configuration for system prompt building."""
        config = await get_business_config_impl(business_id)
        if config is None:
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        return config

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def update_business_info(
        business_id: str,
        field: str,
        value: Any,
    ) -> dict[str, Any]:
        """Update a single business configuration field."""
        return await update_business_info_impl(business_id, field, value)

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def upsert_business_context(
        business_id: str,
        path: str,
        value: Any,
    ) -> dict[str, Any]:
        """Merge a fact into free business_context storage (e.g. path=contact.email).

        Use this for extra info that is not an operational column (email, GST, parking,
        social handles). Does not replace the whole JSON — sibling keys are kept.
        """
        return await upsert_business_context_impl(business_id, path, value)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_business_hours(business_id: str) -> dict[str, Any]:
        """Get business hours with today's open/closed status."""
        row = await get_business_row(business_id)
        if not row:
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        hours = _parse_json_field(row["business_hours"]) or {}
        # Default to IST for Indian SMBs
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        today_key = DAY_KEYS[now.weekday()]
        today_hours = hours.get(today_key) if isinstance(hours, dict) else None

        is_open = False
        closes_at = None
        next_open = None
        if isinstance(today_hours, dict) and today_hours.get("open") and today_hours.get("close"):
            closes_at = today_hours["close"]
            try:
                open_t = datetime.strptime(today_hours["open"], "%H:%M").time()
                close_t = datetime.strptime(today_hours["close"], "%H:%M").time()
                is_open = open_t <= now.time() <= close_t
            except ValueError:
                is_open = False

        if not is_open:
            for i in range(1, 8):
                idx = (now.weekday() + i) % 7
                key = DAY_KEYS[idx]
                h = hours.get(key) if isinstance(hours, dict) else None
                if isinstance(h, dict) and h.get("open"):
                    next_open = {"day": key, "open": h["open"]}
                    break

        return {
            "hours": hours,
            "today": today_key,
            "is_open_now": is_open,
            "closes_at": closes_at,
            "next_open": next_open,
        }
