-- 016_web_widget.sql
-- Stage 8: public web chat channel + admin audit trail

ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'web_widget';

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id UUID REFERENCES admin_users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    business_id UUID REFERENCES businesses(id) ON DELETE SET NULL,
    entity_type TEXT,
    entity_id TEXT,
    detail JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_ai_specialist
    ON messages ((ai_metadata->>'specialist'))
    WHERE role = 'ai';
