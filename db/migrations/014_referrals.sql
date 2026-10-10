-- 014_referrals.sql
-- Referral tracking for signup links (RLS tenant-scoped)

CREATE TABLE referrals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    referral_code TEXT NOT NULL,
    referred_by_business_id UUID REFERENCES businesses(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_referrals_business ON referrals(business_id);
CREATE INDEX idx_referrals_code ON referrals(referral_code);

ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_referrals ON referrals
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
