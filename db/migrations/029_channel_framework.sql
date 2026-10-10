ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'gmail';
ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'outlook';
ALTER TYPE channel_type ADD VALUE IF NOT EXISTS 'slack';

CREATE TABLE IF NOT EXISTS connected_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (
        platform IN (
            'gmail', 'outlook', 'slack', 'google_calendar',
            'razorpay', 'fast2sms'
        )
    ),
    auth_type TEXT NOT NULL DEFAULT 'oauth2'
        CHECK (auth_type IN ('oauth2', 'basic', 'api_key')),
    access_token_encrypted BYTEA NOT NULL,
    refresh_token_encrypted BYTEA,
    token_expires_at TIMESTAMPTZ,
    scopes TEXT[] NOT NULL DEFAULT '{}',
    account_identifier TEXT,
    account_display_name TEXT,
    capabilities TEXT[] NOT NULL DEFAULT '{}',
    is_communication_channel BOOLEAN NOT NULL DEFAULT false,
    channel_config JSONB NOT NULL DEFAULT '{
        "send_mode": "threshold",
        "auto_send_threshold": 0.85,
        "approval_threshold": 0.60,
        "notify_on_auto_send": false
    }'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    connection_error TEXT,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (business_id, platform)
);

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    connected_account_id UUID NOT NULL
        REFERENCES connected_accounts(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    subscription_id TEXT,
    endpoint_url TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    renewal_interval_hours INT,
    last_renewed_at TIMESTAMPTZ,
    next_renewal_at TIMESTAMPTZ,
    renewal_failures INT NOT NULL DEFAULT 0,
    last_event_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'renewal_failed', 'disabled')),
    platform_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (business_id, platform)
);

CREATE TABLE IF NOT EXISTS channel_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('gmail', 'outlook', 'slack')),
    direction TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    sender_id TEXT NOT NULL,
    sender_display TEXT,
    content TEXT,
    content_html TEXT,
    subject TEXT,
    thread_id TEXT,
    platform_message_id TEXT NOT NULL,
    in_reply_to TEXT,
    attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence_score NUMERIC(3,2)
        CHECK (confidence_score BETWEEN 0 AND 1),
    send_action TEXT CHECK (
        send_action IS NULL OR send_action IN (
            'auto_send', 'send_and_notify', 'queued', 'owner_sent'
        )
    ),
    approved_by TEXT CHECK (
        approved_by IS NULL OR approved_by IN ('auto', 'owner')
    ),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (business_id, channel, platform_message_id)
);

CREATE INDEX IF NOT EXISTS idx_connected_accounts_business
    ON connected_accounts(business_id) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_connected_accounts_platform
    ON connected_accounts(platform, is_active);
CREATE INDEX IF NOT EXISTS idx_connected_accounts_channel
    ON connected_accounts(business_id, is_communication_channel)
    WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_webhook_subs_renewal
    ON webhook_subscriptions(next_renewal_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_webhook_subs_business
    ON webhook_subscriptions(business_id);
CREATE INDEX IF NOT EXISTS idx_channel_messages_business
    ON channel_messages(business_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_channel_messages_customer
    ON channel_messages(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_channel_messages_thread
    ON channel_messages(business_id, channel, thread_id);
CREATE INDEX IF NOT EXISTS idx_channel_messages_pending
    ON channel_messages(business_id, send_action)
    WHERE direction = 'outbound' AND send_action = 'queued';

ALTER TABLE connected_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS connected_accounts_tenant ON connected_accounts;
CREATE POLICY connected_accounts_tenant ON connected_accounts
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
DROP POLICY IF EXISTS webhook_subscriptions_tenant ON webhook_subscriptions;
CREATE POLICY webhook_subscriptions_tenant ON webhook_subscriptions
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
DROP POLICY IF EXISTS channel_messages_tenant ON channel_messages;
CREATE POLICY channel_messages_tenant ON channel_messages
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
