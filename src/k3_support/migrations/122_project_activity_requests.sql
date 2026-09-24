CREATE TABLE project_activity_requests (
    activity_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    actor TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES project_bug_grants(grant_id),
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    reader_digest TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('history','comments')),
    end_time_ms INTEGER NOT NULL CHECK(end_time_ms>=0),
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
CREATE UNIQUE INDEX one_pending_activity_per_bug_kind ON project_activity_requests(bug_id,kind)
WHERE state IN ('queued','running');
CREATE TRIGGER project_activity_identity_immutable BEFORE UPDATE ON project_activity_requests
WHEN NEW.activity_id IS NOT OLD.activity_id OR NEW.bug_id IS NOT OLD.bug_id
 OR NEW.actor IS NOT OLD.actor OR NEW.grant_id IS NOT OLD.grant_id
 OR NEW.request_id IS NOT OLD.request_id OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.end_time_ms IS NOT OLD.end_time_ms
 OR NEW.created_at IS NOT OLD.created_at OR NEW.kind IS NOT OLD.kind
 OR (OLD.state='succeeded' AND (NEW.result_json IS NOT OLD.result_json OR NEW.state IS NOT OLD.state))
BEGIN SELECT RAISE(ABORT,'history identity and accepted observation are immutable'); END;
CREATE TRIGGER project_activity_retention BEFORE DELETE ON project_activity_requests
BEGIN SELECT RAISE(ABORT,'operation history requires controlled retention'); END;
