CREATE TABLE retention_purge_reconciliations (
    request_id TEXT PRIMARY KEY REFERENCES retention_purge_requests(request_id),
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('keep_file','confirm_absence')),
    observation_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
