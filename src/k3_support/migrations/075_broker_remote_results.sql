CREATE TABLE broker_remote_results (
    request_id TEXT PRIMARY KEY REFERENCES broker_remote_actions(request_id),
    exit_code INTEGER NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
