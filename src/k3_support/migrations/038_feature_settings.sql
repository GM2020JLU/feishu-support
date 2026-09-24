CREATE TABLE feature_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL,
    base_digest TEXT NOT NULL,
    values_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE feature_settings_history (
    revision INTEGER PRIMARY KEY,
    base_digest TEXT NOT NULL,
    values_json TEXT NOT NULL,
    previous_json TEXT NOT NULL,
    draft_id TEXT NOT NULL UNIQUE,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE feature_settings_drafts (
    draft_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    base_digest TEXT NOT NULL,
    previous_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_revision INTEGER
);
