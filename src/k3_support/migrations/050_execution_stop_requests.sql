CREATE TABLE execution_stop_requests (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    binding_digest TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    previous_state TEXT NOT NULL,
    board_session_id TEXT,
    requested_at TEXT NOT NULL
);
