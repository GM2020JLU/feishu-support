CREATE TABLE broker_results (
    grant_id TEXT PRIMARY KEY REFERENCES broker_grants(grant_id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    attempt_no INTEGER NOT NULL CHECK(attempt_no>0),
    lifecycle_round INTEGER NOT NULL CHECK(lifecycle_round>0),
    input_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK(length(result_digest)=64),
    result_text TEXT NOT NULL CHECK(length(result_text)<=200000),
    sections_json TEXT NOT NULL CHECK(json_valid(sections_json)),
    received_at TEXT NOT NULL,
    UNIQUE(job_id,attempt_no)
);
