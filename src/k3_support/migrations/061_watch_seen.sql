CREATE TABLE watch_seen (
    action_id TEXT PRIMARY KEY REFERENCES watch_actions(action_id),
    owner_id TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
