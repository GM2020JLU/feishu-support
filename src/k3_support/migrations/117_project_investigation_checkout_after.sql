CREATE TABLE project_investigation_checkout_after (
 request_id TEXT PRIMARY KEY REFERENCES broker_remote_actions(request_id),
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 state TEXT NOT NULL CHECK(state IN ('clean','dirty','mismatch','unavailable')),
 observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
 recorded_at TEXT NOT NULL
);
CREATE INDEX investigation_checkout_after_job ON project_investigation_checkout_after(job_id);
CREATE TRIGGER investigation_checkout_after_immutable
BEFORE UPDATE ON project_investigation_checkout_after
BEGIN SELECT RAISE(ABORT,'post-command observations are immutable'); END;
CREATE TRIGGER investigation_checkout_after_retention
BEFORE DELETE ON project_investigation_checkout_after
BEGIN SELECT RAISE(ABORT,'post-command observations require controlled retention'); END;
