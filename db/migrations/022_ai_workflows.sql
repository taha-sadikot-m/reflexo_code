-- 022_ai_workflows.sql
-- AI-built Temporal workflows (Stage 12). Existing workflow_rules stay unchanged.

CREATE TABLE ai_workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    description TEXT,
    nl_description TEXT,

    trigger_type workflow_trigger NOT NULL,
    trigger_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
    steps JSONB NOT NULL,
    response_template TEXT,

    instance_key TEXT,
    max_duration_seconds INT NOT NULL DEFAULT 604800,
    continue_as_new BOOLEAN NOT NULL DEFAULT false,
    temporal_workflow_id TEXT,

    requires_owner_confirmation BOOLEAN NOT NULL DEFAULT false,
    max_executions_per_day INT NOT NULL DEFAULT 1000,

    is_active BOOLEAN NOT NULL DEFAULT true,
    is_tested BOOLEAN NOT NULL DEFAULT false,
    created_by TEXT NOT NULL DEFAULT 'ai',

    total_runs INT NOT NULL DEFAULT 0,
    successful_runs INT NOT NULL DEFAULT 0,
    failed_runs INT NOT NULL DEFAULT 0,
    avg_latency_ms FLOAT,
    last_run_at TIMESTAMPTZ,

    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_workflows_business
    ON ai_workflows(business_id)
    WHERE is_active = true;
CREATE INDEX idx_ai_workflows_trigger
    ON ai_workflows(business_id, trigger_type)
    WHERE is_active = true;

ALTER TABLE ai_workflows ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_ai_workflows ON ai_workflows;
CREATE POLICY tenant_isolation_ai_workflows ON ai_workflows
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());

CREATE TABLE ai_workflow_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    workflow_id UUID NOT NULL REFERENCES ai_workflows(id) ON DELETE CASCADE,

    trigger_event JSONB NOT NULL,
    conversation_id UUID REFERENCES conversations(id),
    customer_id UUID REFERENCES customers(id),

    temporal_run_id TEXT,
    temporal_workflow_instance_id TEXT,
    steps_executed JSONB NOT NULL DEFAULT '[]'::jsonb,

    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    total_latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ai_workflow_runs_status_check CHECK (
        status IN ('pending', 'running', 'success', 'failed', 'timeout', 'cancelled')
    )
);

CREATE INDEX idx_ai_workflow_runs_workflow ON ai_workflow_runs(workflow_id, created_at DESC);
CREATE INDEX idx_ai_workflow_runs_business ON ai_workflow_runs(business_id, created_at DESC);
CREATE INDEX idx_ai_workflow_runs_status ON ai_workflow_runs(business_id, status);

ALTER TABLE ai_workflow_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_ai_workflow_runs ON ai_workflow_runs;
CREATE POLICY tenant_isolation_ai_workflow_runs ON ai_workflow_runs
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
