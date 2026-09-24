CREATE TABLE broker_remote_actions (
    request_id TEXT PRIMARY KEY,
    peer_uid INTEGER NOT NULL,
    grant_id TEXT NOT NULL REFERENCES broker_grants(grant_id),
    request_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','unknown','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
