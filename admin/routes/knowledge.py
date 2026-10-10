"""Knowledge base admin."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from admin.auth import AdminPlus, CurrentAdmin
from admin.db import get_admin_pool
from admin.helpers import audit, require_business, row_dict
from admin.knowledge_ops import ignore_gap, resolve_gap
from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.search.hybrid_rrf import hybrid_rrf_search
from mcp_servers.reflexo_core.utils.cache import cache_delete_prefix
from mcp_servers.reflexo_core.utils.db_helpers import vec_literal

router = APIRouter(prefix="/api/admin/knowledge", tags=["admin-knowledge"])


class KnowledgeCreate(BaseModel):
    answer: str = Field(min_length=1)
    question: str | None = None
    category: str = "general"
    source: str = "admin"


class KnowledgeUpdate(BaseModel):
    answer: str | None = None
    question: str | None = None
    category: str | None = None
    is_active: bool | None = None


class SearchTestBody(BaseModel):
    query: str = Field(min_length=1)
    category: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class ResolveGapBody(BaseModel):
    answer: str = Field(min_length=1)
    category: str = "general"


@router.get("/{business_id}")
async def list_knowledge(
    business_id: str,
    admin: CurrentAdmin,
    q: str | None = None,
    category: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_inactive: bool = False,
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    pool = await get_admin_pool()
    clauses = ["business_id = $1::uuid"]
    params: list[Any] = [business_id]
    if not include_inactive:
        clauses.append("is_active = true")
    if category:
        params.append(category)
        clauses.append(f"category = ${len(params)}")
    if q:
        params.append(f"%{q.strip()}%")
        clauses.append(f"(question ILIKE ${len(params)} OR answer ILIKE ${len(params)})")
    where = " AND ".join(clauses)
    total = await pool.fetchval(f"SELECT COUNT(*) FROM knowledge_entries WHERE {where}", *params)
    params.extend([limit, offset])
    rows = await pool.fetch(
        f"""
        SELECT id, question, answer, category, source, confidence, is_active,
               usage_count, last_used_at, created_at, updated_at
        FROM knowledge_entries
        WHERE {where}
        ORDER BY updated_at DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )
    return {"items": [row_dict(r) for r in rows], "total": int(total or 0)}


@router.post("/{business_id}")
async def add_knowledge(
    business_id: str, body: KnowledgeCreate, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    text = f"{body.question or ''} {body.answer}".strip()
    try:
        embedding = await embed(text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}") from exc
    vec = vec_literal(embedding)
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO knowledge_entries (business_id, question, answer, category, embedding, source)
        VALUES ($1::uuid, $2, $3, $4, $5::vector, $6)
        RETURNING id, question, answer, category, source, created_at
        """,
        business_id,
        body.question,
        body.answer.strip(),
        body.category,
        vec,
        body.source,
    )
    await cache_delete_prefix(f"kb_cache:{business_id}:")
    await audit(admin.id, "knowledge.create", business_id=business_id, entity_id=str(row["id"]))
    return row_dict(row)


@router.put("/{business_id}/{entry_id}")
async def update_knowledge(
    business_id: str, entry_id: str, body: KnowledgeUpdate, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    existing = await pool.fetchrow(
        "SELECT * FROM knowledge_entries WHERE id = $1::uuid AND business_id = $2::uuid",
        entry_id,
        business_id,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    new_q = body.question if body.question is not None else existing["question"]
    new_a = body.answer if body.answer is not None else existing["answer"]
    new_cat = body.category if body.category is not None else existing["category"]
    new_active = body.is_active if body.is_active is not None else existing["is_active"]
    embedding_sql = "embedding"
    params: list[Any] = [new_q, new_a, new_cat, new_active, entry_id, business_id]
    if body.answer is not None or body.question is not None:
        embedding = await embed(f"{new_q or ''} {new_a}".strip())
        embedding_sql = "$7::vector"
        params.append(vec_literal(embedding))
    await pool.execute(
        f"""
        UPDATE knowledge_entries
        SET question = $1, answer = $2, category = $3, is_active = $4,
            embedding = {embedding_sql}, updated_at = now()
        WHERE id = $5::uuid AND business_id = $6::uuid
        """,
        *params,
    )
    await cache_delete_prefix(f"kb_cache:{business_id}:")
    await audit(admin.id, "knowledge.update", business_id=business_id, entity_id=entry_id)
    return {"id": entry_id, "status": "updated"}


@router.delete("/{business_id}/{entry_id}")
async def delete_knowledge(
    business_id: str, entry_id: str, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    result = await pool.execute(
        """
        UPDATE knowledge_entries
        SET is_active = false, updated_at = now()
        WHERE id = $1::uuid AND business_id = $2::uuid
        """,
        entry_id,
        business_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Knowledge entry not found")
    await cache_delete_prefix(f"kb_cache:{business_id}:")
    await audit(admin.id, "knowledge.delete", business_id=business_id, entity_id=entry_id)
    return {"id": entry_id, "status": "deactivated"}


@router.post("/{business_id}/search-test")
async def search_test(
    business_id: str, body: SearchTestBody, admin: CurrentAdmin
) -> dict[str, Any]:
    del admin
    await require_business(business_id)
    try:
        embedding = await embed(body.query.strip(), task_type="retrieval_query")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Embedding failed: {exc}") from exc
    pool = await get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.current_business_id', $1, false)",
            business_id,
        )
        rows = await hybrid_rrf_search(
            conn, embedding, body.query.strip(), body.limit, body.category
        )
    return {
        "results": [
            {
                "id": str(r["id"]),
                "question": r["question"],
                "answer": r["answer"],
                "category": r["category"],
                "rrf_score": float(r["rrf_score"] or 0),
            }
            for r in rows
        ]
    }


@router.post("/{business_id}/gaps/{gap_id}/resolve")
async def resolve_knowledge_gap(
    business_id: str, gap_id: str, body: ResolveGapBody, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    result = await resolve_gap(
        pool,
        business_id=business_id,
        gap_id=gap_id,
        answer=body.answer,
        category=body.category,
    )
    await audit(
        admin.id,
        "knowledge.gap_resolve",
        business_id=business_id,
        entity_id=gap_id,
        detail={"knowledge_entry_id": result.get("knowledge_entry_id")},
    )
    return result


@router.post("/{business_id}/gaps/{gap_id}/ignore")
async def ignore_knowledge_gap(
    business_id: str, gap_id: str, admin: AdminPlus
) -> dict[str, Any]:
    await require_business(business_id)
    pool = await get_admin_pool()
    result = await ignore_gap(pool, business_id=business_id, gap_id=gap_id)
    await audit(
        admin.id,
        "knowledge.gap_ignore",
        business_id=business_id,
        entity_id=gap_id,
    )
    return result
