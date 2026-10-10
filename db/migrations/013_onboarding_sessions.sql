-- 013_onboarding_sessions.sql
-- Conversational onboarding progress tracking (RLS tenant-scoped)

CREATE TABLE onboarding_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    -- Progress tracking
    progress JSONB NOT NULL DEFAULT '{}',
    current_step INT DEFAULT 1,

    -- Status
    is_completed BOOLEAN DEFAULT false,
    is_paused BOOLEAN DEFAULT false,

    -- Timestamps
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    last_activity_at TIMESTAMPTZ DEFAULT now(),

    -- Resumption / reminders (Celery deferred; columns reserved)
    reminder_count INT DEFAULT 0,
    last_reminder_at TIMESTAMPTZ,

    -- Referral context (signup API deferred)
    referral_code TEXT,
    referral_business_id UUID REFERENCES businesses(id),

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_onboarding_business ON onboarding_sessions(business_id);
CREATE INDEX idx_onboarding_incomplete ON onboarding_sessions(business_id)
    WHERE is_completed = false;
CREATE INDEX idx_onboarding_stale ON onboarding_sessions(last_activity_at)
    WHERE is_completed = false AND is_paused = false;

ALTER TABLE onboarding_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_onboarding_sessions ON onboarding_sessions
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
