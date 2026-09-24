CREATE TABLE broker_launches (
    claim_request_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('launching','accepted','unknown','finished')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
