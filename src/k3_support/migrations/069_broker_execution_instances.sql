-- Control supervisor observations only. Never backfill historical executions.
CREATE TABLE broker_execution_instances (
    grant_id TEXT PRIMARY KEY REFERENCES broker_execution_starts(grant_id),
    claim_request_id TEXT NOT NULL UNIQUE,
    unit_name TEXT NOT NULL UNIQUE,
    invocation_id TEXT NOT NULL UNIQUE,
    cgroup_path TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TRIGGER broker_execution_instances_no_update
BEFORE UPDATE ON broker_execution_instances BEGIN
    SELECT RAISE(ABORT,'execution instance binding is immutable');
END;
