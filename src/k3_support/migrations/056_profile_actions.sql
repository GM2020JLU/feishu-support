CREATE TABLE profile_actions (
    request_id TEXT PRIMARY KEY,
    requester_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    proposal_json TEXT NOT NULL,
    previous_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
