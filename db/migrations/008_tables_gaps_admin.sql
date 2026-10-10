-- 008_tables_gaps_admin.sql
-- Knowledge gaps, test accounts, admin users, usage events

CREATE TABLE knowledge_gaps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    question TEXT NOT NULL,
    context TEXT,
    conversation_id UUID REFERENCES conversations(id),
    customer_id UUID REFERENCES customers(id),

    status gap_status DEFAULT 'open',
    answer TEXT,
    knowledge_entry_id UUID REFERENCES knowledge_entries(id),

    escalated_at TIMESTAMPTZ,
    escalation_channel channel_type,
    owner_responded_at TIMESTAMPTZ,

    occurrence_count INT DEFAULT 1,
    last_occurred_at TIMESTAMPTZ DEFAULT now(),

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_gaps_business_status ON knowledge_gaps(business_id, status);
CREATE INDEX idx_gaps_business_count
    ON knowledge_gaps(business_id, occurrence_count DESC)
    WHERE status = 'open';

CREATE TABLE test_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    test_phone TEXT,
    test_email TEXT DEFAULT 'test@reflexo.ai',
    test_ig_id TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE(business_id)
);

CREATE TABLE admin_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'support',
    is_active BOOLEAN DEFAULT true,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    channel channel_type,
    quantity INT DEFAULT 1,
    estimated_cost_inr NUMERIC(8, 4),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_usage_business_date ON usage_events(business_id, created_at);
CREATE INDEX idx_usage_business_type ON usage_events(business_id, event_type, created_at);
