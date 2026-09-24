CREATE TABLE notification_snooze (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    until_at TEXT,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE notification_snooze_history (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    revision INTEGER NOT NULL UNIQUE,
    until_at TEXT,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
