CREATE TABLE broker_remote_observations (
    observation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES broker_remote_actions(request_id),
    snapshot_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','observed','unknown','stale')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX broker_remote_observations_request ON broker_remote_observations(request_id,created_at);
