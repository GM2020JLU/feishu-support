CREATE TABLE retention_attempts (
    attempt_id TEXT PRIMARY KEY,
    event_pk TEXT NOT NULL REFERENCES inbound_events(event_pk),
    original_path TEXT NOT NULL,
    quarantine_path TEXT NOT NULL UNIQUE,
    file_stamp_json TEXT NOT NULL CHECK(json_valid(file_stamp_json)),
    received_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('prepared','quarantined','restored','cancelled','failed')),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX retention_one_live_attempt ON retention_attempts(event_pk)
WHERE state IN ('prepared','quarantined');
