CREATE TABLE broker_inputs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL
);
