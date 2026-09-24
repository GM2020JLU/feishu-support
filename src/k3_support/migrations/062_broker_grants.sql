CREATE TABLE broker_grants (
    grant_id TEXT PRIMARY KEY,
    token_digest TEXT NOT NULL UNIQUE CHECK(length(token_digest)=64 AND token_digest NOT GLOB '*[^0-9a-f]*'),
    worker_uid INTEGER NOT NULL CHECK(worker_uid>0 AND worker_uid<4294967295),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    attempt_no INTEGER NOT NULL CHECK(attempt_no>0),
    lifecycle_round INTEGER NOT NULL CHECK(lifecycle_round>0),
    input_digest TEXT NOT NULL,
    lease_owner TEXT NOT NULL CHECK(length(lease_owner)>0),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE(job_id,attempt_no)
);
CREATE INDEX idx_broker_grants_worker ON broker_grants(worker_uid,job_id);
