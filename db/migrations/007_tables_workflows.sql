-- 007_tables_workflows.sql
-- Persistent automation rules and execution log

CREATE TABLE workflow_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    description TEXT,
    trigger_type workflow_trigger NOT NULL,
    trigger_config JSONB DEFAULT '{}',
    conditions JSONB DEFAULT '[]',
    action_sequence JSONB NOT NULL,

    requires_owner_confirmation BOOLEAN DEFAULT false,
    skip_if_bot_suspected BOOLEAN DEFAULT true,
    max_executions_per_day INT DEFAULT 1000,

    created_by TEXT DEFAULT 'ai',

    is_active BOOLEAN DEFAULT true,
    is_tested BOOLEAN DEFAULT false,

    total_executions INT DEFAULT 0,
    successful_executions INT DEFAULT 0,
    failed_executions INT DEFAULT 0,
    avg_latency_ms FLOAT,
    last_executed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_workflow_rules_business
    ON workflow_rules(business_id)
    WHERE is_active = true;
CREATE INDEX idx_workflow_rules_trigger
    ON workflow_rules(business_id, trigger_type)
    WHERE is_active = true;

CREATE TABLE workflow_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    rule_id UUID NOT NULL REFERENCES workflow_rules(id) ON DELETE CASCADE,

    trigger_event JSONB NOT NULL,
    conversation_id UUID REFERENCES conversations(id),
    customer_id UUID REFERENCES customers(id),

    actions_executed JSONB DEFAULT '[]',

    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    total_latency_ms INT,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_workflow_exec_rule ON workflow_executions(rule_id, created_at DESC);
CREATE INDEX idx_workflow_exec_business ON workflow_executions(business_id, created_at DESC);
CREATE INDEX idx_workflow_exec_status ON workflow_executions(business_id, status);
