-- 005_tables_knowledge.sql
-- Knowledge base, structured entities, document ingestion metadata
-- IVFFlat indexes are created in 010_seed.sql after seed rows exist.

CREATE TABLE knowledge_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    question TEXT,
    answer TEXT NOT NULL,
    category TEXT DEFAULT 'general',

    embedding vector(768) NOT NULL,
    search_text TEXT GENERATED ALWAYS AS (
        COALESCE(question, '') || ' ' || answer
    ) STORED,
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', COALESCE(question, '') || ' ' || answer)
    ) STORED,

    source TEXT DEFAULT 'owner',
    source_conversation_id UUID,
    confidence FLOAT DEFAULT 1.0,

    is_active BOOLEAN DEFAULT true,
    usage_count INT DEFAULT 0,
    last_used_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_knowledge_tsv ON knowledge_entries USING gin(tsv);
CREATE INDEX idx_knowledge_business ON knowledge_entries(business_id) WHERE is_active = true;
CREATE INDEX idx_knowledge_category ON knowledge_entries(business_id, category) WHERE is_active = true;

CREATE TABLE structured_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    entity_type entity_type NOT NULL,

    name TEXT NOT NULL,
    description TEXT,
    data JSONB NOT NULL DEFAULT '{}',

    embedding vector(768),
    search_text TEXT GENERATED ALWAYS AS (
        name || ' ' || COALESCE(description, '')
    ) STORED,
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', name || ' ' || COALESCE(description, ''))
    ) STORED,

    is_active BOOLEAN DEFAULT true,
    sort_order INT DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_entities_business_type
    ON structured_entities(business_id, entity_type)
    WHERE is_active = true;
CREATE INDEX idx_entities_tsv ON structured_entities USING gin(tsv);
CREATE INDEX idx_entities_data ON structured_entities USING gin(data);

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    filename TEXT NOT NULL,
    file_url TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size_bytes BIGINT,

    status document_status DEFAULT 'pending',
    extracted_text TEXT,
    chunks_created INT DEFAULT 0,
    processing_error TEXT,

    uploaded_by TEXT DEFAULT 'owner',

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_documents_business ON documents(business_id);
CREATE INDEX idx_documents_status ON documents(business_id, status);
