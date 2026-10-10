-- 013_gap_sms_followup.sql
-- Track high-priority escalation SMS follow-up (§6.3)

ALTER TABLE knowledge_gaps
    ADD COLUMN IF NOT EXISTS sms_followup_sent_at TIMESTAMPTZ;
