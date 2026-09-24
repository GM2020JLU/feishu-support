CREATE TABLE broker_board_results (
    request_id TEXT PRIMARY KEY REFERENCES broker_board_actions(request_id),
    exit_code INTEGER NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    received_at TEXT NOT NULL
);
