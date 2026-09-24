CREATE TABLE project_comment_dispatch_requests (
    dispatch_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES project_bug_operations(operation_id),
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    runtime_digest TEXT NOT NULL,
    reader_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','blocked','failed')),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    error_code TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor,request_id),
    CHECK(state!='running' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK(state!='succeeded' OR result_json IS NOT NULL)
);
CREATE UNIQUE INDEX one_pending_comment_dispatch ON project_comment_dispatch_requests(operation_id)
WHERE state IN ('queued','running');
CREATE TRIGGER comment_dispatch_identity BEFORE UPDATE ON project_comment_dispatch_requests
WHEN NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.operation_id IS NOT OLD.operation_id
 OR NEW.actor IS NOT OLD.actor OR NEW.request_id IS NOT OLD.request_id
 OR NEW.request_digest IS NOT OLD.request_digest OR NEW.runtime_digest IS NOT OLD.runtime_digest
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.state IN ('succeeded','blocked','failed') AND
     (NEW.state IS NOT OLD.state OR NEW.result_json IS NOT OLD.result_json OR NEW.error_code IS NOT OLD.error_code))
BEGIN SELECT RAISE(ABORT,'comment dispatch intent and completed result are immutable'); END;
CREATE TRIGGER comment_dispatch_retention BEFORE DELETE ON project_comment_dispatch_requests
BEGIN SELECT RAISE(ABORT,'comment dispatch requires controlled retention'); END;
