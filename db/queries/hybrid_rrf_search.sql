-- Reciprocal Rank Fusion (RRF) search over knowledge_entries
-- k = 60 (standard RRF constant)
--
-- Parameters:
--   $1 = query embedding (vector)
--   $2 = query text (string)
--   $3 = limit (int)
--
-- Requires app.current_business_id to be set (RLS).

WITH semantic AS (
    SELECT id,
           ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank_s
    FROM knowledge_entries
    WHERE business_id = current_business_id()
      AND is_active = true
    ORDER BY embedding <=> $1::vector
    LIMIT 20
),
fulltext AS (
    SELECT id,
           ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, plainto_tsquery('english', $2)) DESC) AS rank_f
    FROM knowledge_entries
    WHERE business_id = current_business_id()
      AND is_active = true
      AND tsv @@ plainto_tsquery('english', $2)
    ORDER BY ts_rank(tsv, plainto_tsquery('english', $2)) DESC
    LIMIT 20
)
SELECT ke.id, ke.question, ke.answer, ke.category, ke.confidence,
       COALESCE(1.0 / (60 + s.rank_s), 0) + COALESCE(1.0 / (60 + f.rank_f), 0) AS rrf_score
FROM knowledge_entries ke
LEFT JOIN semantic s ON ke.id = s.id
LEFT JOIN fulltext f ON ke.id = f.id
WHERE s.id IS NOT NULL OR f.id IS NOT NULL
ORDER BY rrf_score DESC
LIMIT $3;
