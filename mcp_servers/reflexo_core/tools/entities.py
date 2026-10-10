"""Structured entity tools."""

from __future__ import annotations

import json
from typing import Any

from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.search.hybrid_rrf import entity_hybrid_search
from mcp_servers.reflexo_core.utils.db_helpers import business_exists, row_to_dict, tenant_connection, vec_literal
from mcp_servers.reflexo_core.utils.errors import tool_error

VALID_ENTITY_TYPES = {
    "product",
    "service",
    "faq",
    "policy",
    "team_member",
    "location",
    "offering",
    "portfolio_item",
    "event",
    "custom",
}


async def upsert_entity_impl(
    business_id: str,
    entity_type: str,
    name: str,
    data: dict,
    description: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Create or update an entity for MCP and trusted local agents."""
    if entity_type not in VALID_ENTITY_TYPES:
        return tool_error("INVALID_ENTITY_TYPE", f"Unknown entity_type: {entity_type}")
    if not await business_exists(business_id):
        return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

    text = f"{name} {description or ''}".strip()
    try:
        embedding = await embed(text)
    except Exception as exc:
        return tool_error("INVALID_INPUT", f"Embedding failed: {exc}")
    vec = vec_literal(embedding)

    async with tenant_connection(business_id) as conn:
        if entity_id:
            existing = await conn.fetchrow(
                "SELECT data FROM structured_entities WHERE id = $1::uuid AND business_id = $2::uuid",
                entity_id,
                business_id,
            )
            if not existing:
                return tool_error("ENTRY_NOT_FOUND", f"Entity {entity_id} not found")
            old = existing["data"]
            if isinstance(old, str):
                old = json.loads(old)
            merged = {**(old or {}), **data}
            await conn.execute(
                """
                UPDATE structured_entities
                SET name = $1, description = $2, data = $3::jsonb,
                    embedding = $4::vector, entity_type = $5::entity_type, updated_at = now()
                WHERE id = $6::uuid AND business_id = $7::uuid
                """,
                name,
                description,
                json.dumps(merged),
                vec,
                entity_type,
                entity_id,
                business_id,
            )
            return {"id": entity_id, "status": "updated"}

        dup = await conn.fetchval(
            """
            SELECT id FROM structured_entities
            WHERE business_id = $1::uuid AND entity_type = $2::entity_type
              AND lower(name) = lower($3) AND is_active = true
            LIMIT 1
            """,
            business_id,
            entity_type,
            name,
        )
        if dup:
            return tool_error("DUPLICATE_NAME", f"Entity '{name}' of type {entity_type} already exists")

        row = await conn.fetchrow(
            """
            INSERT INTO structured_entities
                (business_id, entity_type, name, description, data, embedding)
            VALUES ($1::uuid, $2::entity_type, $3, $4, $5::jsonb, $6::vector)
            RETURNING id
            """,
            business_id,
            entity_type,
            name,
            description,
            json.dumps(data),
            vec,
        )
        return {"id": str(row["id"]), "status": "created"}


def register(mcp) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def search_entities(
        business_id: str,
        query: str,
        entity_type: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search structured entities using hybrid semantic + keyword search."""
        if not query or not query.strip():
            return tool_error("EMPTY_QUERY", "query is empty string")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        if entity_type and entity_type not in VALID_ENTITY_TYPES:
            return tool_error("INVALID_ENTITY_TYPE", f"Unknown entity_type: {entity_type}")

        limit = max(1, min(limit, 50))
        try:
            embedding = await embed(query.strip(), task_type="retrieval_query")
        except Exception as exc:
            return tool_error("INVALID_INPUT", f"Embedding failed: {exc}")

        async with tenant_connection(business_id) as conn:
            from mcp_servers.reflexo_core.reranker import RERANK_FETCH, rerank_results_async

            rows = await entity_hybrid_search(
                conn, embedding, query.strip(), entity_type, RERANK_FETCH
            )

        results = []
        for r in rows:
            data = r["data"]
            if isinstance(data, str):
                data = json.loads(data)
            results.append(
                {
                    "id": str(r["id"]),
                    "entity_type": r["entity_type"],
                    "name": r["name"],
                    "description": r["description"],
                    "data": data,
                    "relevance_score": float(r["relevance_score"] or 0),
                }
            )
        results, reranked = await rerank_results_async(
            query.strip(), results, top_k=limit
        )
        return {
            "results": results,
            "total_results": len(results),
            "search_type": "hybrid_rrf_rerank" if reranked else "hybrid_rrf",
        }

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def upsert_entity(
        business_id: str,
        entity_type: str,
        name: str,
        data: dict,
        description: str | None = None,
        entity_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a structured entity."""
        return await upsert_entity_impl(
            business_id,
            entity_type,
            name,
            data,
            description,
            entity_id,
        )

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def get_entity(business_id: str, entity_id: str) -> dict[str, Any]:
        """Get a single entity by ID."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        async with tenant_connection(business_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, entity_type, name, description, data, is_active, sort_order, created_at, updated_at
                FROM structured_entities
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                entity_id,
                business_id,
            )
        if not row:
            return tool_error("ENTRY_NOT_FOUND", f"Entity {entity_id} not found")
        return row_to_dict(row)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def list_entities(
        business_id: str,
        entity_type: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List all entities of a given type for a business."""
        if entity_type not in VALID_ENTITY_TYPES:
            return tool_error("INVALID_ENTITY_TYPE", f"Unknown entity_type: {entity_type}")
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")

        limit = max(1, min(limit, 100))
        async with tenant_connection(business_id) as conn:
            total = await conn.fetchval(
                """
                SELECT COUNT(*) FROM structured_entities
                WHERE business_id = $1::uuid AND entity_type = $2::entity_type AND is_active = true
                """,
                business_id,
                entity_type,
            )
            rows = await conn.fetch(
                """
                SELECT id, entity_type, name, description, data, sort_order
                FROM structured_entities
                WHERE business_id = $1::uuid AND entity_type = $2::entity_type AND is_active = true
                ORDER BY sort_order, name
                LIMIT $3 OFFSET $4
                """,
                business_id,
                entity_type,
                limit,
                offset,
            )
        entities = [row_to_dict(r) for r in rows]
        return {
            "entities": entities,
            "total_count": int(total or 0),
            "has_more": (offset + limit) < int(total or 0),
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def delete_entity(business_id: str, entity_id: str) -> dict[str, Any]:
        """Soft-delete an entity (sets is_active = false)."""
        if not await business_exists(business_id):
            return tool_error("BUSINESS_NOT_FOUND", f"No business found with ID {business_id}")
        async with tenant_connection(business_id) as conn:
            result = await conn.execute(
                """
                UPDATE structured_entities
                SET is_active = false, updated_at = now()
                WHERE id = $1::uuid AND business_id = $2::uuid
                """,
                entity_id,
                business_id,
            )
            if result == "UPDATE 0":
                return tool_error("ENTRY_NOT_FOUND", f"Entity {entity_id} not found")
        return {"id": entity_id, "status": "deactivated"}
