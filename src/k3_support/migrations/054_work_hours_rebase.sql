ALTER TABLE work_hours_drafts ADD COLUMN source_base_digest TEXT;
CREATE TABLE work_hours_rebases (
    revision INTEGER PRIMARY KEY REFERENCES work_hours_history(revision),
    source_base_digest TEXT NOT NULL,
    target_base_digest TEXT NOT NULL,
    target_timezone TEXT NOT NULL
);
