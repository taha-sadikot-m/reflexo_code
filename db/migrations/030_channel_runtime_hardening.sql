ALTER TYPE workflow_trigger ADD VALUE IF NOT EXISTS 'slack_message';

ALTER TABLE channel_messages
    ADD COLUMN IF NOT EXISTS processing_status TEXT NOT NULL DEFAULT 'completed'
        CHECK (processing_status IN ('processing', 'completed', 'failed')),
    ADD COLUMN IF NOT EXISTS processing_attempts INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_connected_accounts_platform_identifier
    ON connected_accounts(platform, account_identifier)
    WHERE account_identifier IS NOT NULL AND is_active = true;
