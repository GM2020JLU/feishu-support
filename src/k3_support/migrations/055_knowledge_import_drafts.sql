CREATE TABLE knowledge_import_drafts (
    draft_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    applied_by TEXT,
    applied_at TEXT,
    result_json TEXT
);
