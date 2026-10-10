DROP INDEX IF EXISTS idx_connected_accounts_platform_identifier;
CREATE UNIQUE INDEX idx_connected_accounts_platform_identifier
    ON connected_accounts(platform, lower(account_identifier))
    WHERE account_identifier IS NOT NULL AND is_active = true;

ALTER TABLE channel_messages
    ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ
        NOT NULL DEFAULT now();
