CREATE TABLE broker_board_cleanup (
    grant_id TEXT PRIMARY KEY REFERENCES broker_grants(grant_id),
    session_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running','succeeded','unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
