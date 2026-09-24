-- Intents precede broker submission; no retrospective adoption of success logs.
CREATE TABLE project_verification_runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES project_verification_plans(plan_id),
    step_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES broker_grants(grant_id),
    remote_request_id TEXT NOT NULL UNIQUE,
    remote_json TEXT NOT NULL CHECK(json_valid(remote_json)),
    created_at TEXT NOT NULL,
    UNIQUE(actor, request_id)
);
CREATE TRIGGER project_verification_runs_immutable_update
BEFORE UPDATE ON project_verification_runs BEGIN
    SELECT RAISE(ABORT, 'verification run intents are immutable');
END;
CREATE TRIGGER project_verification_runs_immutable_delete
BEFORE DELETE ON project_verification_runs BEGIN
    SELECT RAISE(ABORT, 'verification runs require controlled retention');
END;
