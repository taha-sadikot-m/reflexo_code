-- 018_cross_channel_sessions.sql
-- Voice+WhatsApp bridge audit trail (Redis is the hot-path lookup)

CREATE TABLE IF NOT EXISTS cross_channel_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id),
    voice_session_id TEXT NOT NULL,
    prompt_message TEXT NOT NULL,
    expected_content TEXT,
    status TEXT NOT NULL DEFAULT 'waiting',
    received_content JSONB,
    received_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '1 hour'),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cross_channel_customer
    ON cross_channel_sessions(customer_id, status);
CREATE INDEX IF NOT EXISTS idx_cross_channel_business
    ON cross_channel_sessions(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cross_channel_voice_session
    ON cross_channel_sessions(voice_session_id, status);

ALTER TABLE cross_channel_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_cross_channel_sessions ON cross_channel_sessions;
CREATE POLICY tenant_isolation_cross_channel_sessions ON cross_channel_sessions
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
