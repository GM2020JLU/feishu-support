CREATE TABLE project_link_intakes (
    intake_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    url TEXT NOT NULL,
    identity_json TEXT NOT NULL CHECK(json_valid(identity_json)),
    reader_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    grant_expires_at TEXT NOT NULL,
    local_priority TEXT NOT NULL CHECK(local_priority IN ('P0','P1','P2','P3')),
    state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','blocked')),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at TEXT,
    bug_id TEXT REFERENCES project_bugs(bug_id),
    grant_id TEXT REFERENCES project_bug_grants(grant_id),
    snapshot_id TEXT REFERENCES project_bug_snapshots(snapshot_id),
    reused INTEGER NOT NULL DEFAULT 0 CHECK(reused IN (0,1)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor,request_id),
    CHECK(state!='running' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
    CHECK(state!='succeeded' OR (bug_id IS NOT NULL AND grant_id IS NOT NULL AND snapshot_id IS NOT NULL))
);
CREATE UNIQUE INDEX one_active_intake_per_link ON project_link_intakes(url)
WHERE state IN ('queued','running');
CREATE TRIGGER project_link_intake_identity_immutable
BEFORE UPDATE ON project_link_intakes
WHEN NEW.intake_id IS NOT OLD.intake_id OR NEW.actor IS NOT OLD.actor
 OR NEW.request_id IS NOT OLD.request_id OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.url IS NOT OLD.url OR NEW.identity_json IS NOT OLD.identity_json
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.expires_at IS NOT OLD.expires_at
 OR NEW.grant_expires_at IS NOT OLD.grant_expires_at OR NEW.created_at IS NOT OLD.created_at
 OR NEW.local_priority IS NOT OLD.local_priority
BEGIN SELECT RAISE(ABORT,'link intake identity is immutable'); END;
CREATE TRIGGER project_link_intake_retention
BEFORE DELETE ON project_link_intakes
BEGIN SELECT RAISE(ABORT,'link intakes require controlled retention'); END;
