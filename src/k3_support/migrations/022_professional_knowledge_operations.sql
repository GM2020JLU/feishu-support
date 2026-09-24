CREATE TABLE professional_source_change_events (
    event_id TEXT PRIMARY KEY,
    input_digest TEXT NOT NULL,
    repository TEXT NOT NULL,
    change_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    changed_path TEXT NOT NULL,
    knowledge_revision_id TEXT NOT NULL
        REFERENCES professional_knowledge_revisions(revision_id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL
        REFERENCES professional_knowledge_claims(claim_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    previous_lifecycle_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(input_digest, knowledge_revision_id, claim_id, source_id, changed_path)
);

CREATE INDEX idx_professional_source_change_revision
ON professional_source_change_events(knowledge_revision_id, created_at DESC);

CREATE INDEX idx_professional_source_change_claim
ON professional_source_change_events(claim_id, created_at DESC);
