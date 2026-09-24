CREATE TABLE knowledge_uses (
    use_id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL REFERENCES knowledge_entries(knowledge_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    outbox_id TEXT NOT NULL UNIQUE REFERENCES outbox(outbox_id),
    state TEXT NOT NULL CHECK(state IN ('delivered','helpful','corrected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(knowledge_id, case_id, outbox_id)
);

CREATE INDEX idx_knowledge_uses_case ON knowledge_uses(case_id, created_at);
