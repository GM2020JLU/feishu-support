CREATE TABLE project_refresh_requests (
    refresh_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES project_bug_grants(grant_id),
    request_digest TEXT NOT NULL,
    reader_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','blocked')),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    snapshot_id TEXT REFERENCES project_bug_snapshots(snapshot_id),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(state!='running' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK(state!='succeeded' OR snapshot_id IS NOT NULL),
    UNIQUE(actor,request_id)
);
CREATE UNIQUE INDEX one_active_refresh_per_bug ON project_refresh_requests(bug_id)
WHERE state IN ('queued','running');
CREATE TRIGGER project_refresh_identity_immutable
BEFORE UPDATE ON project_refresh_requests
WHEN NEW.refresh_id IS NOT OLD.refresh_id OR NEW.bug_id IS NOT OLD.bug_id
 OR NEW.actor IS NOT OLD.actor OR NEW.request_id IS NOT OLD.request_id
 OR NEW.grant_id IS NOT OLD.grant_id OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'refresh identity is immutable'); END;
CREATE TRIGGER project_refresh_retention
BEFORE DELETE ON project_refresh_requests
BEGIN SELECT RAISE(ABORT,'refresh requests require controlled retention'); END;
