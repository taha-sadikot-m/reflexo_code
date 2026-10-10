-- 011_gap_embeddings.sql
-- Vector column for knowledge_gaps similarity (log_gap duplicate detection)

ALTER TABLE knowledge_gaps
    ADD COLUMN IF NOT EXISTS embedding vector(768);

-- IVFFlat needs rows; create with lists=1 (rebuild at scale)
CREATE INDEX IF NOT EXISTS idx_gaps_embedding ON knowledge_gaps
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1);
