CREATE TABLE chat_history_runs (
    run_id TEXT PRIMARY KEY,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','complete','partial','failed')),
    archive_dir TEXT NOT NULL,
    total_chats INTEGER NOT NULL DEFAULT 0,
    completed_chats INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    matched_message_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    manifest_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE chat_history_chats (
    run_id TEXT NOT NULL REFERENCES chat_history_runs(run_id) ON DELETE CASCADE,
    chat_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','complete','failed')),
    archive_file TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    matched_message_count INTEGER NOT NULL DEFAULT 0,
    content_digest TEXT,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, chat_id)
);

CREATE INDEX idx_chat_history_chats_state ON chat_history_chats(run_id,state);
