CREATE TABLE doc_history_runs (
    run_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('running','complete','partial','failed')),
    archive_dir TEXT NOT NULL,
    total_documents INTEGER NOT NULL DEFAULT 0,
    completed_documents INTEGER NOT NULL DEFAULT 0,
    skipped_documents INTEGER NOT NULL DEFAULT 0,
    k3_documents INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    manifest_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE doc_history_documents (
    run_id TEXT NOT NULL REFERENCES doc_history_runs(run_id) ON DELETE CASCADE,
    stable_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    doc_type TEXT,
    title TEXT,
    url TEXT,
    token TEXT,
    revision_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','complete','skipped','failed')),
    archive_file TEXT,
    content_chars INTEGER NOT NULL DEFAULT 0,
    k3_term_hits INTEGER NOT NULL DEFAULT 0,
    content_digest TEXT,
    error_code TEXT,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, stable_id)
);

CREATE INDEX idx_doc_history_state ON doc_history_documents(run_id,state);
CREATE INDEX idx_doc_history_k3 ON doc_history_documents(run_id,k3_term_hits);
