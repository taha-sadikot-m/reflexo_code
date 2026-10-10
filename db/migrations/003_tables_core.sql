-- 003_tables_core.sql
-- Tenant root + connected channels

CREATE TABLE businesses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE,
    description TEXT,
    vertical TEXT DEFAULT 'general',
    owner_name TEXT,
    owner_phone TEXT NOT NULL,
    owner_email TEXT,

    business_hours JSONB DEFAULT '{}',
    address JSONB DEFAULT '{}',
    website TEXT,

    business_context JSONB DEFAULT '{}',

    persona_name TEXT DEFAULT 'AI Assistant',
    persona_tone TEXT DEFAULT 'friendly',
    persona_language TEXT[] DEFAULT '{en,hi}',
    custom_instructions TEXT,
    greeting_message TEXT,

    plan subscription_plan DEFAULT 'free',
    plan_status subscription_status DEFAULT 'active',
    message_limit INT DEFAULT 100,
    messages_used INT DEFAULT 0,
    billing_cycle_start TIMESTAMPTZ DEFAULT now(),

    onboarding_completed BOOLEAN DEFAULT false,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_businesses_owner_phone ON businesses(owner_phone);
CREATE INDEX idx_businesses_slug ON businesses(slug);
CREATE INDEX idx_businesses_plan ON businesses(plan) WHERE is_active = true;

CREATE TABLE business_channels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    channel channel_type NOT NULL,

    channel_account_id TEXT NOT NULL,
    channel_display_name TEXT,

    access_token_encrypted BYTEA,
    refresh_token_encrypted BYTEA,
    token_expires_at TIMESTAMPTZ,

    webhook_verify_token TEXT,
    webhook_secret TEXT,

    is_active BOOLEAN DEFAULT true,
    last_synced_at TIMESTAMPTZ,
    error_message TEXT,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE(business_id, channel, channel_account_id)
);

CREATE INDEX idx_business_channels_lookup
    ON business_channels(channel, channel_account_id)
    WHERE is_active = true;
CREATE INDEX idx_business_channels_business ON business_channels(business_id);
