CREATE TABLE project_bug_operations (
    operation_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    snapshot_id TEXT NOT NULL REFERENCES project_bug_snapshots(snapshot_id),
    grant_id TEXT NOT NULL REFERENCES project_bug_grants(grant_id),
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    bug_revision INTEGER NOT NULL,
    case_version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('bug.fields','bug.comment','bug.transition','bug.close')),
    change_json TEXT NOT NULL CHECK(json_valid(change_json)),
    state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN
        ('prepared','dispatched','unknown','confirmed','rejected','partial','cancelled','conflict','satisfied')),
    write_json TEXT CHECK(write_json IS NULL OR json_valid(write_json)),
    write_digest TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor, request_id),
    CHECK((write_json IS NULL) = (write_digest IS NULL))
);
-- Unknown side effects block new writes regardless of lease or credential expiry.
CREATE UNIQUE INDEX project_bug_one_unsettled_write ON project_bug_operations(bug_id)
WHERE state IN ('prepared','dispatched','unknown');
CREATE TRIGGER project_bug_operation_identity_immutable
BEFORE UPDATE OF operation_id,bug_id,snapshot_id,grant_id,actor,request_id,request_digest,
    bug_revision,case_version,action,change_json,created_at ON project_bug_operations BEGIN
    SELECT RAISE(ABORT, 'Bug operation identity is immutable');
END;
CREATE TRIGGER project_bug_operation_write_immutable
BEFORE UPDATE OF write_json,write_digest ON project_bug_operations
WHEN OLD.write_json IS NOT NULL BEGIN
    SELECT RAISE(ABORT, 'dispatched Bug write is immutable');
END;
CREATE TRIGGER project_bug_operation_transition
BEFORE UPDATE OF state ON project_bug_operations
WHEN NOT (NEW.state=OLD.state
    OR (OLD.state='prepared' AND NEW.state IN ('dispatched','cancelled','conflict','satisfied'))
    OR (OLD.state IN ('dispatched','unknown') AND NEW.state IN ('unknown','confirmed','rejected','partial')))
BEGIN SELECT RAISE(ABORT, 'invalid Bug operation transition'); END;

CREATE TABLE project_bug_operation_observations (
    observation_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES project_bug_operations(operation_id),
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    created_at TEXT NOT NULL
);
