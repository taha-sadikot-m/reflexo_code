-- 002_enums.sql
-- Custom enum types for REFLEXO

CREATE TYPE subscription_plan AS ENUM ('free', 'starter', 'premium');
CREATE TYPE subscription_status AS ENUM ('active', 'past_due', 'cancelled', 'trial');
CREATE TYPE channel_type AS ENUM (
    'whatsapp',
    'instagram_dm',
    'instagram_comment',
    'facebook_messenger',
    'facebook_comment',
    'email',
    'voice',
    'sms'
);
CREATE TYPE message_role AS ENUM ('customer', 'ai', 'owner', 'system');
CREATE TYPE workflow_trigger AS ENUM (
    'whatsapp_message',
    'instagram_dm',
    'instagram_comment',
    'facebook_message',
    'facebook_comment',
    'email_received',
    'voice_call',
    'payment_received',
    'payment_failed',
    'order_created',
    'schedule',
    'manual'
);
CREATE TYPE gap_status AS ENUM ('open', 'escalated', 'resolved', 'ignored');
CREATE TYPE document_status AS ENUM ('pending', 'processing', 'completed', 'failed');
CREATE TYPE entity_type AS ENUM (
    'product',
    'service',
    'faq',
    'policy',
    'team_member',
    'location',
    'offering',
    'portfolio_item',
    'event',
    'custom'
);
