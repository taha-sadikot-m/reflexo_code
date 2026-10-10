ALTER TABLE businesses
    ADD COLUMN IF NOT EXISTS persona JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS smart_number_default TEXT NOT NULL DEFAULT 'ask_owner';

ALTER TABLE business_channels
    ADD COLUMN IF NOT EXISTS meta_page_data JSONB NOT NULL DEFAULT '{}'::jsonb;
