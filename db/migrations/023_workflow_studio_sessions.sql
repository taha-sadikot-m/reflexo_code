-- 023_workflow_studio_sessions.sql
-- Token-scoped owner review, stateful testing, and activation for AI workflows.

CREATE TABLE workflow_studio_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    workflow_id UUID NOT NULL REFERENCES ai_workflows(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '48 hours'),
    status TEXT NOT NULL DEFAULT 'draft',
    visual_data JSONB NOT NULL DEFAULT '{"nodes":[],"edges":[]}'::jsonb,
    test_results JSONB,
    created_by TEXT NOT NULL DEFAULT 'ai',
    created_by_admin UUID REFERENCES admin_users(id),
    edit_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT workflow_studio_status_check
        CHECK (status IN ('draft', 'testing', 'tested', 'active', 'expired'))
);

CREATE INDEX idx_workflow_studio_token
    ON workflow_studio_sessions(token);
CREATE INDEX idx_workflow_studio_business
    ON workflow_studio_sessions(business_id, created_at DESC);
CREATE INDEX idx_workflow_studio_workflow
    ON workflow_studio_sessions(workflow_id, created_at DESC);

ALTER TABLE workflow_studio_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_workflow_studio_sessions
    ON workflow_studio_sessions
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
