CREATE TABLE broker_execution_starts (
    grant_id TEXT PRIMARY KEY REFERENCES broker_grants(grant_id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    attempt_no INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    peer_uid INTEGER NOT NULL,
    authorized_at TEXT NOT NULL,
    UNIQUE(job_id,attempt_no),
    UNIQUE(peer_uid,request_id)
);
