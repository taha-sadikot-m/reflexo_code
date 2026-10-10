-- Staged go-live for canonical Temporal workflows.

ALTER TABLE ai_workflows
    ADD COLUMN IF NOT EXISTS shadow_mode BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS shadow_allowlist JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE ai_workflow_runs
    ADD COLUMN IF NOT EXISTS is_shadow BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_ai_workflows_shadow
    ON ai_workflows(business_id, shadow_mode)
    WHERE is_active = true;
