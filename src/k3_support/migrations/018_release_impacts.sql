CREATE TABLE release_impacts (
    impact_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    change_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    subject TEXT NOT NULL,
    branch TEXT,
    changed_paths_json TEXT NOT NULL CHECK(json_valid(changed_paths_json)),
    input_digest TEXT NOT NULL UNIQUE,
    assessment_json TEXT NOT NULL CHECK(json_valid(assessment_json)),
    outbox_id TEXT REFERENCES outbox(outbox_id),
    created_at TEXT NOT NULL,
    UNIQUE(repository, change_id, revision)
);

CREATE INDEX idx_release_impacts_created ON release_impacts(created_at);
