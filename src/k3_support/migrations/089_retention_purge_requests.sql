CREATE TABLE retention_purge_requests (
    request_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES retention_attempts(attempt_id),
    actor_id TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    days INTEGER NOT NULL CHECK(days BETWEEN 1 AND 3650),
    state TEXT NOT NULL CHECK(state IN ('prepared','running','unknown','purged','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX retention_one_pending_purge ON retention_purge_requests(attempt_id)
WHERE state IN ('prepared','running','unknown','purged');
