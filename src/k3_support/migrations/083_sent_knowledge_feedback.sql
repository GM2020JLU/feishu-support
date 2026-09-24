CREATE TABLE sent_knowledge_feedback (
    request_id TEXT PRIMARY KEY,
    use_id TEXT NOT NULL REFERENCES knowledge_uses(use_id),
    actor_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('helpful','incorrect','incomplete')),
    content_digest TEXT NOT NULL,
    entry_fingerprint TEXT NOT NULL,
    review_state TEXT NOT NULL CHECK(review_state IN ('recorded','pending')),
    created_at TEXT NOT NULL,
    UNIQUE(use_id,actor_id,verdict,content_digest)
);
