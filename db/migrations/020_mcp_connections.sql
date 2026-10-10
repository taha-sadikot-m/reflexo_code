-- 020_mcp_connections.sql
-- Per-business custom MCP servers for the Universal MCP Connector (Stage 11)

CREATE TABLE mcp_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    description TEXT,
    server_url TEXT NOT NULL,
    transport_type TEXT NOT NULL DEFAULT 'http',
    auth_config JSONB NOT NULL DEFAULT '{}'::jsonb,

    is_active BOOLEAN NOT NULL DEFAULT true,
    last_health_check TIMESTAMPTZ,
    last_health_status TEXT,
    discovered_tools JSONB NOT NULL DEFAULT '[]'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (business_id, name),
    CONSTRAINT mcp_connections_transport_http CHECK (transport_type = 'http'),
    CONSTRAINT mcp_connections_name_slug CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$')
);

CREATE INDEX idx_mcp_connections_business
    ON mcp_connections(business_id)
    WHERE is_active = true;

ALTER TABLE mcp_connections ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_mcp_connections ON mcp_connections;
CREATE POLICY tenant_isolation_mcp_connections ON mcp_connections
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
