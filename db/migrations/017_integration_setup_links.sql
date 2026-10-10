-- 017_integration_setup_links.sql
-- Shareable, hashed tokens for owner-facing channel/tool setup pages.

CREATE TABLE IF NOT EXISTS integration_setup_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    created_by UUID REFERENCES admin_users(id) ON DELETE SET NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '7 days'),
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_setup_links_business_active
    ON integration_setup_links(business_id)
    WHERE revoked_at IS NULL;
