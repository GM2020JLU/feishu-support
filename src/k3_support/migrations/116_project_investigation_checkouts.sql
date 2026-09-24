CREATE TABLE project_investigation_checkouts (
 request_id TEXT PRIMARY KEY REFERENCES broker_remote_actions(request_id),
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
 recorded_at TEXT NOT NULL
);
CREATE TRIGGER investigation_checkout_immutable
BEFORE UPDATE ON project_investigation_checkouts
BEGIN SELECT RAISE(ABORT,'checkout observations are immutable'); END;
CREATE TRIGGER investigation_checkout_retention
BEFORE DELETE ON project_investigation_checkouts
BEGIN SELECT RAISE(ABORT,'checkout observations require controlled retention'); END;
CREATE INDEX investigation_checkout_job ON project_investigation_checkouts(job_id);
