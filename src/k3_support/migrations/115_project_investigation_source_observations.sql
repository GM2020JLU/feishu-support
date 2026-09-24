CREATE TABLE project_investigation_source_observations (
    observation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES broker_remote_actions(request_id),
    state TEXT NOT NULL CHECK(state IN ('matched','mismatch','unavailable')),
    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
    recorded_at TEXT NOT NULL
);
CREATE INDEX investigation_source_request ON project_investigation_source_observations(request_id);
CREATE TRIGGER investigation_source_observation_immutable
BEFORE UPDATE ON project_investigation_source_observations
BEGIN SELECT RAISE(ABORT,'source observations are immutable'); END;
CREATE TRIGGER investigation_source_observation_retention
BEFORE DELETE ON project_investigation_source_observations
BEGIN SELECT RAISE(ABORT,'source observations require controlled retention'); END;
