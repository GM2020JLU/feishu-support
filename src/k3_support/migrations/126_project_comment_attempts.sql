-- One immutable native invocation reservation per already-dispatched operation.
-- This table does not queue, approve, or authorize a mutation.
CREATE TABLE project_comment_attempts (
    operation_id TEXT PRIMARY KEY REFERENCES project_bug_operations(operation_id),
    write_digest TEXT NOT NULL,
    reader_digest TEXT NOT NULL,
    user_key TEXT NOT NULL,
    baseline_json TEXT NOT NULL CHECK(json_valid(baseline_json)),
    state TEXT NOT NULL CHECK(state IN ('reserved','acknowledged','unknown')),
    comment_id TEXT,
    response_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- The accepted 2026-09-17 acknowledgement carries no comment identifier;
    -- attribution happens on reconciliation, so acknowledged rows may keep a
    -- NULL comment_id while still requiring the acknowledgement digest.
    CHECK(comment_id IS NULL OR state='acknowledged'),
    CHECK(state!='acknowledged' OR response_digest IS NOT NULL)
);
CREATE TRIGGER project_comment_attempt_identity BEFORE UPDATE ON project_comment_attempts
WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.write_digest IS NOT OLD.write_digest
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.baseline_json IS NOT OLD.baseline_json
 OR NEW.user_key IS NOT OLD.user_key
 OR NEW.created_at IS NOT OLD.created_at
 OR OLD.state!='reserved'
 OR NEW.state NOT IN ('acknowledged','unknown')
BEGIN SELECT RAISE(ABORT,'comment attempt and receipt are immutable'); END;
CREATE TRIGGER project_comment_attempt_retention BEFORE DELETE ON project_comment_attempts
BEGIN SELECT RAISE(ABORT,'comment attempts require controlled retention'); END;
