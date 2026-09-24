CREATE TABLE project_search_requests (
    search_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
    keyword TEXT NOT NULL,
    after_id INTEGER NOT NULL CHECK(after_id>=0),
    reader_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','blocked')),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor,request_id),
    CHECK(state!='running' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK(state!='succeeded' OR result_json IS NOT NULL)
);
CREATE INDEX project_search_pending ON project_search_requests(state,created_at);
CREATE TRIGGER project_search_identity_immutable BEFORE UPDATE ON project_search_requests
WHEN NEW.search_id IS NOT OLD.search_id OR NEW.actor IS NOT OLD.actor
 OR NEW.request_id IS NOT OLD.request_id OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.scope_json IS NOT OLD.scope_json OR NEW.keyword IS NOT OLD.keyword
 OR NEW.after_id IS NOT OLD.after_id OR NEW.reader_digest IS NOT OLD.reader_digest
 OR NEW.expires_at IS NOT OLD.expires_at OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'search identity is immutable'); END;
CREATE TRIGGER project_search_retention BEFORE DELETE ON project_search_requests
BEGIN SELECT RAISE(ABORT,'search requests require controlled retention'); END;
