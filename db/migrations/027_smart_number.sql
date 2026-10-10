ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS contact_type TEXT NOT NULL DEFAULT 'unknown',
    ADD CONSTRAINT customers_contact_type_check
        CHECK (contact_type IN ('unknown', 'personal', 'business'));

ALTER TABLE businesses
    ADD COLUMN IF NOT EXISTS smart_number_mode BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_customers_personal_contacts
    ON customers(business_id, contact_type)
    WHERE contact_type = 'personal';
