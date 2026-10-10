"""Shared knowledge-gap resolve/ignore used by admin routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from admin.helpers import row_dict
from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.utils.cache import cache_delete_prefix
from mcp_servers.reflexo_core.utils.db_helpers import vec_literal

CLOSED_STATUSES = frozenset({"resolved", "ignored"})

GAP_LIST_COLUMNS = """
    id, question, context, status::text AS status, occurrence_count,
    conversation_id, last_occurred_at, created_at, answer, knowledge_entry_id
"""


async def resolve_gap(
    pool: Any,
    *,
    business_id: str,
    gap_id: str,
    answer: str,
    category: str = "general",
    source: str = "admin",
) -> dict[str, Any]:
    cleaned = (answer or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Answer is required")
    gap = await pool.fetchrow(
        """
        SELECT id, question, status::text AS status
        FROM knowledge_gaps
        WHERE id = $1::uuid AND business_id = $2::uuid
        """,
        gap_id,
        business_id,
    )
    if not gap:
        raise HTTPException(status_code=404, detail="Knowledge gap not found")
    status = str(gap["status"] or "")
    if status in CLOSED_STATUSES:
        raise HTTPException(status_code=409, detail=f"Gap is already {status}")

    text = f"{gap['question'] or ''} {cleaned}".strip()
    try:
        embedding = await embed(text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}") from exc

    kb = await pool.fetchrow(
        """
        INSERT INTO knowledge_entries (business_id, question, answer, category, embedding, source)
        VALUES ($1::uuid, $2, $3, $4, $5::vector, $6)
        RETURNING id, question, answer, category, source
        """,
        business_id,
        gap["question"],
        cleaned,
        category,
        vec_literal(embedding),
        source,
    )
    await pool.execute(
        """
        UPDATE knowledge_gaps
        SET status = 'resolved', answer = $1, knowledge_entry_id = $2::uuid,
            owner_responded_at = now(), updated_at = now()
        WHERE id = $3::uuid AND business_id = $4::uuid
        """,
        cleaned,
        kb["id"],
        gap_id,
        business_id,
    )
    await cache_delete_prefix(f"kb_cache:{business_id}:")
    return {
        "gap_id": gap_id,
        "status": "resolved",
        "knowledge_entry_id": str(kb["id"]),
        "entry": row_dict(kb),
    }


async def ignore_gap(pool: Any, *, business_id: str, gap_id: str) -> dict[str, Any]:
    gap = await pool.fetchrow(
        """
        SELECT id, status::text AS status
        FROM knowledge_gaps
        WHERE id = $1::uuid AND business_id = $2::uuid
        """,
        gap_id,
        business_id,
    )
    if not gap:
        raise HTTPException(status_code=404, detail="Knowledge gap not found")
    status = str(gap["status"] or "")
    if status in CLOSED_STATUSES:
        raise HTTPException(status_code=409, detail=f"Gap is already {status}")
    await pool.execute(
        """
        UPDATE knowledge_gaps
        SET status = 'ignored', updated_at = now()
        WHERE id = $1::uuid AND business_id = $2::uuid
        """,
        gap_id,
        business_id,
    )
    return {"gap_id": gap_id, "status": "ignored"}
