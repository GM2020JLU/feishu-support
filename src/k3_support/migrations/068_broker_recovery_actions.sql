CREATE TABLE broker_recovery_actions (
    request_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    binding_digest TEXT NOT NULL CHECK(length(binding_digest)=64),
    actor_id TEXT NOT NULL CHECK(length(trim(actor_id))>0),
    requested_at TEXT NOT NULL
);
