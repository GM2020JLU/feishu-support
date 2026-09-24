ALTER TABLE execution_stop_requests ADD COLUMN target_json TEXT CHECK(target_json IS NULL OR json_valid(target_json));
CREATE TABLE execution_exit_receipts (
    receipt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    attempt_no INTEGER NOT NULL,
    lease_owner TEXT,
    pid INTEGER NOT NULL,
    process_start_token TEXT NOT NULL,
    returncode INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX execution_exit_target ON execution_exit_receipts(job_id,attempt_no);
