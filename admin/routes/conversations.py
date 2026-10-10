"""Conversations, threads, knowledge gaps."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Query

from admin.auth import CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import require_business, row_dict
from admin.knowledge_ops import GAP_LIST_COLUMNS

router = APIRouter(prefix="/api/admin/conversations", tags=["admin-conversations"])


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _enrich_meta(meta: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(meta, str):
        try:
            decoded = json.loads(meta)
            m = decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            m = {}
    else:
        m = dict(meta or {})
    return {
        "specialist": m.get("specialist"),
        "route_method": m.get("route_method"),
        "route_confidence": _finite_float(
            m.get("route_confidence")
            if m.get("route_confidence") is not None
            else m.get("confidence")
        ),
        "handoff_chain": m.get("handoff_chain") or [],
        "guardrails": m.get("guardrails") or m.get("guardrails_triggered") or [],
        "memories_injected": m.get("memories_injected"),
        "cache_hit": m.get("cache_hit") or m.get("semantic_cache_hit"),
        "latency_ms": _finite_float(m.get("latency_ms")),
        "tokens": m.get("tokens"),
        "estimated_cost_usd": m.get("estimated_cost_usd"),
        "hitl_required": m.get("hitl_required"),
        "raw": m,
    }


@router.get("/{business_id}")
async def list_conversations(
    business_id: str,
    admin: CurrentAdmin,
    channel: str | None = None,
    q: str | None = None,
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    clauses = ["c.business_id = $1::uuid"]
    params: list[Any] = [business_id]
    if channel:
        params.append(channel)
        clauses.append(f"c.channel::text = ${len(params)}")
    if q:
        params.append(f"%{q.strip()}%")
        clauses.append(
            f"(cu.name ILIKE ${len(params)} OR c.summary ILIKE ${len(params)} "
            f"OR c.external_thread_id ILIKE ${len(params)})"
        )
    where = " AND ".join(clauses)
    total = await pool.fetchval(
        f"""
        SELECT COUNT(*)
        FROM conversations c
        LEFT JOIN customers cu ON cu.id = c.customer_id
        WHERE {where}
        """,
        *params,
    )
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""
        SELECT c.id, c.channel, c.is_active, c.is_escalated, c.summary,
               c.message_count, c.last_message_at, c.started_at,
               cu.name AS customer_name, cu.id AS customer_id,
               (
                 SELECT content FROM messages m
                 WHERE m.conversation_id = c.id
                 ORDER BY created_at DESC LIMIT 1
               ) AS last_message,
               (
                 SELECT ai_metadata FROM messages m
                 WHERE m.conversation_id = c.id AND m.role = 'ai'
                 ORDER BY created_at DESC LIMIT 1
               ) AS last_ai_metadata
        FROM conversations c
        LEFT JOIN customers cu ON cu.id = c.customer_id
        WHERE {where}
        ORDER BY c.last_message_at DESC NULLS LAST
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    items = []
    for r in rows:
        d = row_dict(r)
        d["ai_metadata"] = _enrich_meta(d.pop("last_ai_metadata", None) or {})
        items.append(d)
    return {"items": items, "total": int(total or 0), "limit": limit, "offset": offset}


@router.get("/{business_id}/{conversation_id}/messages")
async def conversation_messages(
    business_id: str,
    conversation_id: str,
    admin: CurrentAdmin,
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    conv = await pool.fetchrow(
        """
        SELECT c.*, cu.name AS customer_name
        FROM conversations c
        LEFT JOIN customers cu ON cu.id = c.customer_id
        WHERE c.id = $1::uuid AND c.business_id = $2::uuid
        """,
        conversation_id,
        business_id,
    )
    if not conv:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = await pool.fetch(
        """
        SELECT id, role, content, content_type, media_url, channel,
               ai_metadata, status, error_message, created_at
        FROM messages
        WHERE conversation_id = $1::uuid AND business_id = $2::uuid
        ORDER BY created_at ASC
        """,
        conversation_id,
        business_id,
    )
    out_msgs = []
    for m in msgs:
        d = row_dict(m)
        d["ai_metadata"] = _enrich_meta(d.get("ai_metadata") or {})
        out_msgs.append(d)
    return {"conversation": row_dict(conv), "messages": out_msgs}


@router.get("/{business_id}/gaps")
async def knowledge_gaps(
    business_id: str,
    admin: CurrentAdmin,
    status: str = Query(default="open"),
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    wanted = (status or "open").strip().lower()
    if wanted == "all":
        rows = await pool.fetch(
            f"""
            SELECT {GAP_LIST_COLUMNS}
            FROM knowledge_gaps
            WHERE business_id = $1::uuid
            ORDER BY occurrence_count DESC, last_occurred_at DESC
            """,
            business_id,
        )
    else:
        rows = await pool.fetch(
            f"""
            SELECT {GAP_LIST_COLUMNS}
            FROM knowledge_gaps
            WHERE business_id = $1::uuid AND status::text = $2
            ORDER BY occurrence_count DESC, last_occurred_at DESC
            """,
            business_id,
            wanted,
        )
    return {"items": [row_dict(r) for r in rows]}
