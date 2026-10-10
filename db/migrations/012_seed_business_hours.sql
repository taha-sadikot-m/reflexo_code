-- 012_seed_business_hours.sql
-- Backfill Test Mall Store hours for already-migrated databases

UPDATE businesses
SET business_hours = '{
  "mon": {"open": "10:00", "close": "21:00"},
  "tue": {"open": "10:00", "close": "21:00"},
  "wed": {"open": "10:00", "close": "21:00"},
  "thu": {"open": "10:00", "close": "21:00"},
  "fri": {"open": "10:00", "close": "22:00"},
  "sat": {"open": "10:00", "close": "22:00"},
  "sun": {"open": "11:00", "close": "20:00"}
}'::jsonb,
updated_at = now()
WHERE id = '00000000-0000-0000-0000-000000000001';
