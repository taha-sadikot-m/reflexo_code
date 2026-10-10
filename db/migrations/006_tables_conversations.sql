-- 006_tables_conversations.sql
-- Conversation threads and messages

CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id),
    channel channel_type NOT NULL,

    external_thread_id TEXT,
    subject TEXT,

    is_active BOOLEAN DEFAULT true,
    is_escalated BOOLEAN DEFAULT false,

    summary TEXT,
    extracted_intents TEXT[] DEFAULT '{}',

    message_count INT DEFAULT 0,

    started_at TIMESTAMPTZ DEFAULT now(),
    last_message_at TIMESTAMPTZ DEFAULT now(),
    closed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_conversations_business ON conversations(business_id);
CREATE INDEX idx_conversations_customer ON conversations(business_id, customer_id);
CREATE INDEX idx_conversations_channel ON conversations(business_id, channel);
CREATE INDEX idx_conversations_active
    ON conversations(business_id, is_active, last_message_at DESC);
CREATE INDEX idx_conversations_external ON conversations(channel, external_thread_id);

CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,

    role message_role NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT DEFAULT 'text',
    media_url TEXT,

    channel channel_type NOT NULL,
    external_message_id TEXT,

    ai_metadata JSONB DEFAULT '{}',

    status TEXT DEFAULT 'sent',
    error_message TEXT,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX idx_messages_business ON messages(business_id, created_at DESC);
CREATE INDEX idx_messages_external ON messages(external_message_id);
