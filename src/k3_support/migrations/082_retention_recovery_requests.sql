CREATE TABLE retention_recovery_requests (
    request_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES retention_attempts(attempt_id),
    actor_id TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','restored','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX retention_recovery_by_attempt ON retention_recovery_requests(attempt_id,created_at DESC,request_id DESC);
