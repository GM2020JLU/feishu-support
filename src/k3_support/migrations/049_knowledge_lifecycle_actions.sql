CREATE TABLE knowledge_lifecycle_actions (
    request_id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL REFERENCES knowledge_entries(knowledge_id),
    content_digest TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('candidate','retired')),
    previous_status TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
