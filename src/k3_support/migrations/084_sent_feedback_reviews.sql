CREATE TABLE sent_feedback_reviews (
    request_id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL UNIQUE REFERENCES sent_knowledge_feedback(request_id),
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('needs_revision','dismissed')),
    reason TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
