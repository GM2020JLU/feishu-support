CREATE TABLE work_hours_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    base_digest TEXT NOT NULL,
    values_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE work_hours_drafts (
    draft_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    base_digest TEXT NOT NULL,
    revision INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    applied_revision INTEGER
);
CREATE TABLE work_hours_history (
    revision INTEGER PRIMARY KEY,
    previous_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
