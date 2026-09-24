CREATE TABLE budget_settings_drafts (
    draft_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,
    values_json TEXT NOT NULL CHECK(json_valid(values_json)),
    previous_json TEXT NOT NULL CHECK(json_valid(previous_json)),
    expires_at TEXT NOT NULL,
    applied_revision INTEGER
);
