CREATE TABLE draft_retention_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    base_digest TEXT NOT NULL,
    days INTEGER CHECK(days IS NULL OR days BETWEEN 1 AND 3650),
    updated_at TEXT NOT NULL,
    actor_id TEXT NOT NULL
);
CREATE TABLE draft_retention_policy_drafts (
    draft_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    base_digest TEXT NOT NULL,
    revision INTEGER NOT NULL,
    days INTEGER CHECK(days IS NULL OR days BETWEEN 1 AND 3650),
    expires_at TEXT NOT NULL,
    applied_revision INTEGER
);
CREATE TABLE draft_retention_policy_history (
    revision INTEGER PRIMARY KEY,
    previous_days INTEGER,
    days INTEGER,
    actor_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
