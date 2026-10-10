CREATE TABLE IF NOT EXISTS whatsapp_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    business_id UUID NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    template_name TEXT NOT NULL,
    template_id TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    category TEXT NOT NULL DEFAULT 'UTILITY',
    header_type TEXT,
    header_content TEXT,
    body_text TEXT NOT NULL DEFAULT '',
    footer_text TEXT,
    buttons JSONB NOT NULL DEFAULT '[]'::jsonb,
    template_type TEXT NOT NULL DEFAULT 'standard',
    type_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PENDING',
    rejection_reason TEXT,
    estimated_cost_inr DECIMAL(6,2),
    times_sent INT NOT NULL DEFAULT 0,
    last_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (business_id, template_name, language)
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_templates_business
    ON whatsapp_templates(business_id, status);
CREATE INDEX IF NOT EXISTS idx_whatsapp_templates_category
    ON whatsapp_templates(business_id, category)
    WHERE status = 'APPROVED';

ALTER TABLE whatsapp_templates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation_whatsapp_templates
    ON whatsapp_templates;
CREATE POLICY tenant_isolation_whatsapp_templates
    ON whatsapp_templates
    USING (business_id = current_business_id())
    WITH CHECK (business_id = current_business_id());
