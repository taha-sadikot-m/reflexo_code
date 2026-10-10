-- 004_tables_identity.sql
-- Customers, channel identity map, Reflexo-managed numbers

CREATE TABLE customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,

    name TEXT,
    phone TEXT,
    email TEXT,

    primary_channel channel_type,

    preferences JSONB DEFAULT '{}',
    tags TEXT[] DEFAULT '{}',
    notes TEXT,

    total_messages INT DEFAULT 0,
    total_orders INT DEFAULT 0,
    total_spent NUMERIC(12, 2) DEFAULT 0,
    first_seen_at TIMESTAMPTZ DEFAULT now(),
    last_seen_at TIMESTAMPTZ DEFAULT now(),

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_customers_business ON customers(business_id);
CREATE INDEX idx_customers_phone ON customers(business_id, phone);
CREATE INDEX idx_customers_email ON customers(business_id, email);
CREATE INDEX idx_customers_last_seen ON customers(business_id, last_seen_at DESC);

CREATE TABLE identity_map (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    channel channel_type NOT NULL,
    external_id TEXT NOT NULL,

    display_name TEXT,
    profile_pic_url TEXT,
    is_verified BOOLEAN DEFAULT false,

    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE(business_id, channel, external_id)
);

CREATE INDEX idx_identity_map_lookup ON identity_map(channel, external_id);
CREATE INDEX idx_identity_map_customer ON identity_map(customer_id);
CREATE INDEX idx_identity_map_business ON identity_map(business_id);

CREATE TABLE business_numbers (
    phone_number TEXT PRIMARY KEY,
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    channel channel_type NOT NULL,
    is_reflexo_managed BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_business_numbers_business ON business_numbers(business_id);
