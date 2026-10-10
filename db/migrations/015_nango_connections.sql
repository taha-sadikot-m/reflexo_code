-- 015_nango_connections.sql
-- Map Reflexo business_id → Nango connection_id (OAuth Connect uses random UUIDs)

CREATE TABLE business_nango_connections (
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    integration_id TEXT NOT NULL,
    nango_connection_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (business_id, integration_id)
);

CREATE INDEX idx_business_nango_connections_nango_id
    ON business_nango_connections(nango_connection_id);

ALTER TABLE business_nango_connections ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_business_nango_connections ON business_nango_connections
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
