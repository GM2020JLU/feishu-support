CREATE TABLE broker_board_actions (
    request_id TEXT PRIMARY KEY,
    peer_uid INTEGER NOT NULL,
    grant_id TEXT NOT NULL REFERENCES broker_grants(grant_id),
    session_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    action_json TEXT NOT NULL CHECK(json_valid(action_json)),
    contract_digest TEXT NOT NULL,
    runtime_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','unknown','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX broker_board_actions_grant ON broker_board_actions(grant_id,state);
