-- Registry provenance and OAuth state for remote MCP connections.

ALTER TABLE mcp_connections
    ADD COLUMN registry_name TEXT,
    ADD COLUMN registry_version TEXT,
    ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN auth_status TEXT NOT NULL DEFAULT 'connected';

ALTER TABLE mcp_connections
    ADD CONSTRAINT mcp_connections_auth_type
        CHECK (auth_type IN ('none', 'headers', 'oauth')),
    ADD CONSTRAINT mcp_connections_auth_status
        CHECK (auth_status IN ('connected', 'pending', 'reauth_required'));

CREATE INDEX idx_mcp_connections_registry
    ON mcp_connections(registry_name)
    WHERE registry_name IS NOT NULL;
