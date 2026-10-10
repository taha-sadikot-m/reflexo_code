"""Core product lookups used by the LiveKit voice agent (no MCP round-trip)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.search.hybrid_rrf import entity_hybrid_search, hybrid_rrf_search
from mcp_servers.reflexo_core.tools.config import (
    update_business_info_impl,
    upsert_business_context_impl,
)
from mcp_servers.reflexo_core.tools.entities import VALID_ENTITY_TYPES, upsert_entity_impl
from mcp_servers.reflexo_core.utils.db_helpers import get_business_row, tenant_connection

logger = logging.getLogger(__name__)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
VOICE_SIMPLE_BUSINESS_FIELDS = {
    "name",
    "description",
    "vertical",
    "owner_name",
    "website",
    "persona_name",
    "persona_tone",
    "custom_instructions",
    "greeting_message",
}
_EMAIL_VALUE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$",
    re.IGNORECASE,
)


def _parse_json(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    return val


async def load_business_profile(business_id: str) -> dict[str, Any]:
    row = await get_business_row(business_id)
    if not row:
        return {}
    hours = _parse_json(row["business_hours"]) or {}
    context = _parse_json(row["business_context"]) or {}
    return {
        "name": row["name"] or "the business",
        "persona_name": row["persona_name"] or "the assistant",
        "greeting_message": row["greeting_message"] or "",
        "persona_language": row["persona_language"] or "en",
        "business_hours": hours if isinstance(hours, dict) else {},
        "business_context": context if isinstance(context, dict) else {},
        "description": row["description"] or "",
    }


def format_business_context(profile: dict[str, Any]) -> str:
    bits = [profile.get("description") or ""]
    ctx = profile.get("business_context") or {}
    if isinstance(ctx, dict) and ctx:
        bits.append(json.dumps(ctx, ensure_ascii=False)[:800])
    return "\n".join(b for b in bits if b)


async def search_knowledge_text(business_id: str, query: str, limit: int = 3) -> str:
    try:
        embedding = await embed(query.strip(), task_type="retrieval_query")
        async with tenant_connection(business_id) as conn:
            rows = await hybrid_rrf_search(conn, embedding, query.strip(), max(limit, 5), None)
        if not rows:
            return "I did not find that in the knowledge base."
        parts = []
        for r in rows[:limit]:
            answer = r.get("answer") or ""
            parts.append(str(answer).strip())
        return " ".join(p for p in parts if p) or "I did not find that in the knowledge base."
    except Exception:
        logger.exception("voice search_knowledge failed")
        return "I could not look that up right now."


async def search_entities_text(business_id: str, query: str, limit: int = 3) -> str:
    try:
        embedding = await embed(query.strip(), task_type="retrieval_query")
        async with tenant_connection(business_id) as conn:
            rows = await entity_hybrid_search(conn, embedding, query.strip(), None, max(limit, 5))
        if not rows:
            return "I did not find matching products or services."
        names = []
        for r in rows[:limit]:
            name = r.get("name") or ""
            desc = r.get("description") or ""
            names.append(f"{name}: {desc}".strip(": "))
        return " ".join(names)
    except Exception:
        logger.exception("voice search_entities failed")
        return "I could not search the catalog right now."


async def business_hours_text(business_id: str) -> str:
    row = await get_business_row(business_id)
    if not row:
        return "I do not have the hours loaded."
    hours = _parse_json(row["business_hours"]) or {}
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    today_key = DAY_KEYS[now.weekday()]
    today = hours.get(today_key) if isinstance(hours, dict) else None
    if isinstance(today, dict) and today.get("open") and today.get("close"):
        return (
            f"Today we are open from {today['open']} to {today['close']} India time."
        )
    return "I do not have today's hours on file."


def _spoken_clock(iso: str) -> str:
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = parsed.astimezone(ZoneInfo("Asia/Kolkata"))
        return local.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return iso


def _hours_for_date(hours: dict[str, Any], day: datetime) -> tuple[str, str]:
    today = hours.get(DAY_KEYS[day.weekday()]) if isinstance(hours, dict) else None
    if isinstance(today, dict) and today.get("open") and today.get("close"):
        return str(today["open"]), str(today["close"])
    return "09:00", "18:00"


async def availability_text(
    business_id: str,
    service_name: str,
    date: str = "",
) -> str:
    """Spoken free-slot summary from Google Calendar, or hours fallback."""
    profile = await load_business_profile(business_id)
    hours = profile.get("business_hours") or {}
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    day_raw = (date or "").strip()
    if not day_raw:
        day_raw = now.date().isoformat()
    try:
        day = datetime.fromisoformat(day_raw[:10]).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    except ValueError:
        return "I need a calendar date like 2026-09-08 to check availability."
    start, end = _hours_for_date(hours if isinstance(hours, dict) else {}, day)
    label = (service_name or "that").strip() or "that"

    try:
        from channel_framework.integrations.google_calendar import (
            check_availability as google_check,
        )
    except Exception:
        hours_line = await business_hours_text(business_id)
        return (
            f"I cannot check the booking calendar for {label} right now. {hours_line}"
        )

    try:
        result = await google_check(
            business_id,
            date=day.date().isoformat(),
            duration_minutes=30,
            business_hours_start=start,
            business_hours_end=end,
        )
    except (RuntimeError, ValueError):
        hours_line = await business_hours_text(business_id)
        return (
            f"The booking calendar is not connected, so I cannot check slots for {label}. "
            f"{hours_line}"
        )

    slots = result.get("available_slots") or []
    if not slots:
        return f"I do not see any open 30 minute slots for {label} on {day.date().isoformat()}."
    spoken = ", ".join(_spoken_clock(slot.get("start") or "") for slot in slots[:4] if slot.get("start"))
    extra = " and more later" if len(slots) > 4 else ""
    return f"For {label} on {day.date().isoformat()} I have {spoken}{extra}."


def _mutation_error(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "The update could not be saved.")
    return None


def _spoken_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


async def update_business_info_text(
    business_id: str,
    field: str,
    value: Any,
) -> str:
    if field not in VOICE_SIMPLE_BUSINESS_FIELDS and field not in {
        "business_hours",
        "address",
    }:
        return (
            f"Update failed: {field} is not a supported voice field. "
            "Use business context for extra facts."
        )
    if field in {"business_hours", "address"} and not isinstance(value, dict):
        return f"Update failed: {field.replace('_', ' ')} needs structured details."
    result = await update_business_info_impl(business_id, field, value)
    error = _mutation_error(result)
    if error:
        return f"Update failed: {error}"
    return f"Saved {field.replace('_', ' ')} as {_spoken_value(result['new_value'])}."


async def update_business_context_text(
    business_id: str,
    path: str,
    value: Any,
) -> str:
    normalized_path = ".".join(
        part for part in path.strip().lower().replace("/", ".").split(".") if part
    )
    if normalized_path in {"contact.email", "email"}:
        email = str(value).strip()
        local = email.partition("@")[0]
        if (
            not _EMAIL_VALUE.fullmatch(email)
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
        ):
            return (
                "Update failed: that email address is not valid. "
                "Please spell it again before I save it."
            )
    result = await upsert_business_context_impl(business_id, path, value)
    error = _mutation_error(result)
    if error:
        return f"Update failed: {error}"
    return f"Saved {result['path'].replace('.', ' ')} as {_spoken_value(result['new_value'])}."


async def upsert_catalog_item_text(
    business_id: str,
    entity_type: str,
    name: str,
    data: dict[str, Any],
    description: str | None = None,
) -> str:
    """Update an exact-name item when present, otherwise create it."""
    if entity_type not in VALID_ENTITY_TYPES:
        return f"Catalog update failed: Unknown entity type {entity_type}."
    async with tenant_connection(business_id) as conn:
        existing = await conn.fetchrow(
            """
            SELECT id, description
            FROM structured_entities
            WHERE business_id = $1::uuid
              AND entity_type = $2::entity_type
              AND lower(name) = lower($3)
              AND is_active = true
            LIMIT 1
            """,
            business_id,
            entity_type,
            name.strip(),
        )
    result = await upsert_entity_impl(
        business_id,
        entity_type,
        name.strip(),
        data,
        description if description is not None else (
            str(existing["description"] or "") if existing else None
        ),
        str(existing["id"]) if existing else None,
    )
    error = _mutation_error(result)
    if error:
        return f"Catalog update failed: {error}"
    verb = "Updated" if result["status"] == "updated" else "Created"
    details = ", ".join(
        f"{key.replace('_', ' ')} {_spoken_value(value)}"
        for key, value in data.items()
    )
    suffix = f" with {details}" if details else ""
    return f"{verb} {entity_type} {name.strip()}{suffix}."
