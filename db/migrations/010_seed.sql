-- 010_seed.sql
-- Dev seed data + IVFFlat indexes (created after rows exist)

INSERT INTO admin_users (email, password_hash, name, role) VALUES
('taha@reflexo.ai', crypt('change_me_immediately', gen_salt('bf')), 'Taha', 'super_admin');

INSERT INTO businesses (
    id, name, slug, owner_name, owner_phone, vertical,
    persona_name, persona_tone, greeting_message,
    business_hours,
    plan, message_limit, onboarding_completed
) VALUES (
    '00000000-0000-0000-0000-000000000001',
    'Test Mall Store',
    'test-mall',
    'Test Owner',
    '+919999999999',
    'ecommerce',
    'Maya',
    'friendly',
    'Hi! Welcome to Test Mall Store. How can I help you today?',
    '{
      "mon": {"open": "10:00", "close": "21:00"},
      "tue": {"open": "10:00", "close": "21:00"},
      "wed": {"open": "10:00", "close": "21:00"},
      "thu": {"open": "10:00", "close": "21:00"},
      "fri": {"open": "10:00", "close": "22:00"},
      "sat": {"open": "10:00", "close": "22:00"},
      "sun": {"open": "11:00", "close": "20:00"}
    }'::jsonb,
    'starter',
    10000,
    true
);

-- Set RLS session var so tenant-scoped inserts succeed even if FORCE RLS is on
SELECT set_config(
    'app.current_business_id',
    '00000000-0000-0000-0000-000000000001',
    false
);

INSERT INTO business_numbers (phone_number, business_id, channel) VALUES
('+919999999999', '00000000-0000-0000-0000-000000000001', 'whatsapp');

INSERT INTO test_accounts (business_id, test_phone, test_email) VALUES
('00000000-0000-0000-0000-000000000001', '+910000000000', 'test-mall@reflexo.ai');

-- Seed knowledge with placeholder embeddings (replace with real embeddings in app code)
INSERT INTO knowledge_entries (business_id, question, answer, category, embedding, source) VALUES
(
    '00000000-0000-0000-0000-000000000001',
    'What are your business hours?',
    'We are open Monday to Saturday, 10 AM to 9 PM. We are closed on Sundays.',
    'general',
    (SELECT array_agg(random())::vector(768) FROM generate_series(1, 768)),
    'onboarding'
),
(
    '00000000-0000-0000-0000-000000000001',
    'What is your return policy?',
    'We accept returns within 7 days of purchase with original receipt. Items must be unused and in original packaging.',
    'policy',
    (SELECT array_agg(random())::vector(768) FROM generate_series(1, 768)),
    'onboarding'
),
(
    '00000000-0000-0000-0000-000000000001',
    'Do you offer home delivery?',
    'Yes! We deliver across Mumbai within 2-3 days. Rest of India takes 5-7 business days. Free delivery on orders above ₹999.',
    'shipping',
    (SELECT array_agg(random())::vector(768) FROM generate_series(1, 768)),
    'onboarding'
);

-- IVFFlat needs rows before create. Seed has 3 KB rows — use lists=1 for now.
-- Rebuild with lists=100 (knowledge) / lists=50 (entities) once you have ~10k+ vectors:
--   DROP INDEX IF EXISTS idx_knowledge_embedding;
--   CREATE INDEX idx_knowledge_embedding ON knowledge_entries
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_knowledge_embedding ON knowledge_entries
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1);

-- structured_entities is empty at seed time; IVFFlat cannot build on 0 rows.
-- Create idx_entities_embedding after the first entity insert (lists=1 until scale).
