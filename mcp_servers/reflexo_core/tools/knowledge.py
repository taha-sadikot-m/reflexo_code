"""Knowledge base tools."""

from __future__ import annotations

import hashlib
from typing import Any

from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.search.hybrid_rrf import hybrid_rrf_search
from mcp_servers.reflexo_core.utils.cache import cache_delete_prefix, cache_get_json, cache_set_json
from mcp_servers.reflexo_core.utils.db_helpers import business_exists, tenant_connection, vec_literal
from mcp_servers.reflexo_core.utils.errors import tool_error


def _kb_cache_key(business_id: str, query: str, category: str | None, limit: int) -> str:
    h = hashlib.sha256(f"{query}|{category}|{limit}".encode()).hexdigest()[:16]
    return f"kb_cache:{business_id}:{h}"


def register(mcp) -> None:
    @mcp.tool(
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    async def search_knowledge(
        business_id: str,
        query: str,
        category: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search the business knowledge base using hybrid semantic + keyword search."""
        if not query or not query.strip():
            return tool_error("EMPTY_QUERY", "query is empty string")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        limit = max(1, min(limit, 20))
        cache_key = _kb_cache_key(business_id, query.strip(), category, limit)
        cached = await cache_get_json(cache_key)
        if cached is not None:
            return cached

        try:
            embedding = await embed(query.strip(), task_type="retrieval_query")
        except Exception as exc:
            return tool_error("INVALID_INPUT", f"Embedding failed: {exc}")

        async with tenant_connection(business_id) as conn:
            from mcp_servers.reflexo_core.reranker import RERANK_FETCH, rerank_results_async

            rows = await hybrid_rrf_search(
                conn, embedding, query.strip(), RERANK_FETCH, category
            )
            ids = [r["id"] for r in rows]
            if ids:
                await conn.execute(
                    """
                    UPDATE knowledge_entries
                    SET usage_count = usage_count + 1, last_used_at = now()
                    WHERE id = ANY($1::uuid[])
                    """,
                    ids,
                )

        results = [
            {
                "id": str(r["id"]),
                "question": r["question"],
                "answer": r["answer"],
                "category": r["category"],
                "confidence": float(r["confidence"] or 0),
                "rrf_score": float(r["rrf_score"] or 0),
                "source": r.get("source"),
                "usage_count": int(r.get("usage_count") or 0),
            }
            for r in rows
        ]
        results, reranked = await rerank_results_async(
            query.strip(), results, top_k=limit
        )
        payload = {
            "results": results,
            "total_results": len(results),
            "search_type": "hybrid_rrf_rerank" if reranked else "hybrid_rrf",
        }
        await cache_set_json(cache_key, payload, 60)
        return payload

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def add_knowledge(
        business_id: str,
        answer: str,
        question: str | None = None,
        category: str = "general",
        source: str = "owner",
    ) -> dict[str, Any]:
        """Add a new entry to the business knowledge base."""
        if not answer or not answer.strip():
            return tool_error("EMPTY_ANSWER", "answer is empty")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        text = f"{question or ''} {answer}".strip()
        try:
            embedding = await embed(text)
        except Exception as exc:
            return tool_error("INVALID_INPUT", f"Embedding failed: {exc}")

        vec = vec_literal(embedding)
        async with tenant_connection(business_id) as conn:
            dup = await conn.fetchrow(
                """
                SELECT id, question, answer FROM knowledge_entries
                WHERE business_id = $1::uuid AND is_active = true
                  AND embedding <=> $2::vector < 0.05
                LIMIT 1
                """,
                business_id,
                vec,
            )
            if dup:
                return tool_error(
                    "DUPLICATE_DETECTED",
                    f"Similar entry already exists: {dup['id']}",
                    agent_guidance=f"Update entry {dup['id']} instead of creating a duplicate.",
                )

            row = await conn.fetchrow(
                """
                INSERT INTO knowledge_entries (business_id, question, answer, category, embedding, source)
                VALUES ($1::uuid, $2, $3, $4, $5::vector, $6)
                RETURNING id
                """,
                business_id,
                question,
                answer.strip(),
                category,
                vec,
                source,
            )

        await cache_delete_prefix(f"kb_cache:{business_id}:")
        return {
            "id": str(row["id"]),
            "status": "created",
            "message": "Knowledge entry added successfully",
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def update_knowledge(
        business_id: str,
        entry_id: str,
        answer: str | None = None,
        question: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any]:
        """Update an existing knowledge base entry."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        async with tenant_connection(business_id) as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM knowledge_entries WHERE id = $1::uuid AND business_id = $2::uuid",
                entry_id,
                business_id,
            )
            if not existing:
                return tool_error("ENTRY_NOT_FOUND", f"Knowledge entry {entry_id} not found")

            new_q = question if question is not None else existing["question"]
            new_a = answer if answer is not None else existing["answer"]
            new_cat = category if category is not None else existing["category"]
            new_active = is_active if is_active is not None else existing["is_active"]

            embedding_sql = "embedding"
            params: list[Any] = [new_q, new_a, new_cat, new_active, entry_id, business_id]
            if answer is not None or question is not None:
                text = f"{new_q or ''} {new_a}".strip()
                embedding = await embed(text)
                embedding_sql = "$7::vector"
                params.append(vec_literal(embedding))

            await conn.execute(
                f"""
                UPDATE knowledge_entries
                SET question = $1, answer = $2, category = $3, is_active = $4,
                    embedding = {embedding_sql}, updated_at = now()
                WHERE id = $5::uuid AND business_id = $6::uuid
                """,
                *params,
            )

        await cache_delete_prefix(f"kb_cache:{business_id}:")
        return {"id": entry_id, "status": "updated"}

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def delete_knowledge(business_id: str, entry_id: str) -> dict[str, Any]:
        """Soft-delete a knowledge entry (sets is_active = false)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        async with tenant_connection(business_id) as conn:
            result = await conn.execute(
                """
                UPDATE knowledge_entries
                SET is_active = false, updated_at = now()
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                entry_id,
                business_id,
            )
            if result == "UPDATE 0":
                return tool_error("ENTRY_NOT_FOUND", f"Knowledge entry {entry_id} not found")

        await cache_delete_prefix(f"kb_cache:{business_id}:")
        return {"id": entry_id, "status": "deactivated"}
