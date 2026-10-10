"""Hybrid RRF search over knowledge_entries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg

_RRF_SQL = (Path(__file__).resolve().parents[3] / "db" / "queries" / "hybrid_rrf_search.sql").read_text(
    encoding="utf-8"
)

# Extended query with optional category + usage fields
_RRF_WITH_CATEGORY = """
WITH semantic AS (
    SELECT id,
           ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank_s
    FROM knowledge_entries
    WHERE business_id = current_business_id()
      AND is_active = true
      AND ($4::text IS NULL OR category = $4)
    ORDER BY embedding <=> $1::vector
    LIMIT 20
),
fulltext AS (
    SELECT id,
           ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, plainto_tsquery('english', $2)) DESC) AS rank_f
    FROM knowledge_entries
    WHERE business_id = current_business_id()
      AND is_active = true
      AND ($4::text IS NULL OR category = $4)
      AND tsv @@ plainto_tsquery('english', $2)
    ORDER BY ts_rank(tsv, plainto_tsquery('english', $2)) DESC
    LIMIT 20
)
SELECT ke.id, ke.question, ke.answer, ke.category, ke.confidence, ke.source, ke.usage_count,
       COALESCE(1.0 / (60 + s.rank_s), 0) + COALESCE(1.0 / (60 + f.rank_f), 0) AS rrf_score
FROM knowledge_entries ke
LEFT JOIN semantic s ON ke.id = s.id
LEFT JOIN fulltext f ON ke.id = f.id
WHERE s.id IS NOT NULL OR f.id IS NOT NULL
ORDER BY rrf_score DESC
LIMIT $3
"""


def _vec_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in embedding) + "]"


async def hybrid_rrf_search(
    conn: asyncpg.Connection,
    embedding: list[float],
    query: str,
    limit: int = 5,
    category: str | None = None,
) -> list[dict[str, Any]]:
    vec = _vec_literal(embedding)
    rows = await conn.fetch(_RRF_WITH_CATEGORY, vec, query, limit, category)
    return [dict(r) for r in rows]


async def entity_hybrid_search(
    conn: asyncpg.Connection,
    embedding: list[float],
    query: str,
    entity_type: str | None,
    limit: int,
) -> list[asyncpg.Record]:
    vec = _vec_literal(embedding)
    sql = """
    WITH semantic AS (
        SELECT id,
               ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank_s
        FROM structured_entities
        WHERE business_id = current_business_id()
          AND is_active = true
          AND embedding IS NOT NULL
          AND ($4::text IS NULL OR entity_type::text = $4)
        ORDER BY embedding <=> $1::vector
        LIMIT 20
    ),
    fulltext AS (
        SELECT id,
               ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, plainto_tsquery('english', $2)) DESC) AS rank_f
        FROM structured_entities
        WHERE business_id = current_business_id()
          AND is_active = true
          AND ($4::text IS NULL OR entity_type::text = $4)
          AND tsv @@ plainto_tsquery('english', $2)
        ORDER BY ts_rank(tsv, plainto_tsquery('english', $2)) DESC
        LIMIT 20
    )
    SELECT e.id, e.entity_type, e.name, e.description, e.data,
           COALESCE(1.0 / (60 + s.rank_s), 0) + COALESCE(1.0 / (60 + f.rank_f), 0) AS relevance_score
    FROM structured_entities e
    LEFT JOIN semantic s ON e.id = s.id
    LEFT JOIN fulltext f ON e.id = f.id
    WHERE s.id IS NOT NULL OR f.id IS NOT NULL
    ORDER BY relevance_score DESC
    LIMIT $3
    """
    return await conn.fetch(sql, vec, query, limit, entity_type)
