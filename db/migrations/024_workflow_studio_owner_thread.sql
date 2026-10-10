-- Bind public Workflow Studio chat to the owner conversation that created it.

ALTER TABLE workflow_studio_sessions
    ADD COLUMN IF NOT EXISTS owner_conversation_id UUID
        REFERENCES conversations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_studio_owner_conversation
    ON workflow_studio_sessions(owner_conversation_id)
    WHERE owner_conversation_id IS NOT NULL;
