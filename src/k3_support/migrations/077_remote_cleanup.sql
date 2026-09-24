CREATE TABLE broker_remote_cleanup (
    request_id TEXT PRIMARY KEY REFERENCES broker_remote_actions(request_id),
    observation_id TEXT NOT NULL UNIQUE REFERENCES broker_remote_observations(observation_id),
    observation_snapshot TEXT NOT NULL,
    observation_result TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    action_state TEXT NOT NULL,
    action_updated_at TEXT NOT NULL,
    result_exit_code INTEGER,
    preview_digest TEXT NOT NULL,
    actor_uid INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
