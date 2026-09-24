-- A retirement transaction must write this only with the matching body changes.
-- No scheduler or public write endpoint is enabled by this schema.
CREATE TABLE case_content_retirements (
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    lifecycle_round INTEGER NOT NULL CHECK(lifecycle_round > 0),
    receipt_id TEXT NOT NULL UNIQUE,
    registry_digest TEXT NOT NULL,
    preview_digest TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    PRIMARY KEY(case_id,lifecycle_round)
);
