-- 019_owner_cross_channel_sessions.sql
-- Allow verified business owners (who intentionally have no customer row)
-- to use the Voice+WhatsApp cross-channel bridge.

ALTER TABLE cross_channel_sessions
    ALTER COLUMN customer_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS actor_type TEXT NOT NULL DEFAULT 'customer',
    ADD COLUMN IF NOT EXISTS sender_external_id TEXT;

ALTER TABLE cross_channel_sessions
    DROP CONSTRAINT IF EXISTS cross_channel_sessions_actor_type_check;
ALTER TABLE cross_channel_sessions
    ADD CONSTRAINT cross_channel_sessions_actor_type_check
    CHECK (actor_type IN ('customer', 'owner'));

ALTER TABLE cross_channel_sessions
    DROP CONSTRAINT IF EXISTS cross_channel_sessions_actor_identity_check;
ALTER TABLE cross_channel_sessions
    ADD CONSTRAINT cross_channel_sessions_actor_identity_check
    CHECK (
        (actor_type = 'customer' AND customer_id IS NOT NULL)
        OR
        (actor_type = 'owner' AND customer_id IS NULL AND sender_external_id IS NOT NULL)
    ) NOT VALID;

ALTER TABLE cross_channel_sessions
    VALIDATE CONSTRAINT cross_channel_sessions_actor_identity_check;

CREATE INDEX IF NOT EXISTS idx_cross_channel_owner
    ON cross_channel_sessions(business_id, sender_external_id, status)
    WHERE actor_type = 'owner';
