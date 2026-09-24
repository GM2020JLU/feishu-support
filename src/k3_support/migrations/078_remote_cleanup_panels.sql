CREATE TABLE broker_remote_cleanup_panels (
    token TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    preview_digest TEXT NOT NULL,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    prompt_message_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1))
);
