CREATE TABLE source_refresh_state (
    source_id TEXT PRIMARY KEY REFERENCES source_registry(source_id) ON DELETE CASCADE,
    last_attempt_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    last_error_type TEXT,
    last_state TEXT NOT NULL CHECK(last_state IN ('refreshing','changed','unchanged','failed','superseded')),
    lease_token TEXT,
    lease_expires_at TEXT,
    CHECK((lease_token IS NULL) = (lease_expires_at IS NULL))
);

CREATE INDEX idx_source_refresh_due ON source_refresh_state(next_attempt_at);
