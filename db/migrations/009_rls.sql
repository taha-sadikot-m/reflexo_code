-- 009_rls.sql
-- Row-level security via app.current_business_id session variable
-- businesses and admin_users intentionally have NO RLS

CREATE OR REPLACE FUNCTION current_business_id() RETURNS UUID AS $$
BEGIN
    RETURN current_setting('app.current_business_id', true)::UUID;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'business_channels', 'customers', 'identity_map', 'business_numbers',
            'knowledge_entries', 'structured_entities', 'documents',
            'conversations', 'messages', 'workflow_rules', 'workflow_executions',
            'knowledge_gaps', 'test_accounts', 'usage_events'
        ])
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation_%I ON %I
             USING (business_id = current_business_id())
             WITH CHECK (business_id = current_business_id())',
            tbl, tbl
        );
    END LOOP;
END $$;
