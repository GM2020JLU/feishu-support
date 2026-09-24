-- Field/transition write custody and closure authorization for Project bugs.
-- The shared approvals table enumerates approval types inside CHECK constraints
-- that migrations 026/030/036/052 wired into triggers and foreign keys; adding a
-- type would force rebuilding audited tables. Closure approvals therefore live in
-- the project family with the same lifecycle contract: explicit human decision,
-- action digest binding, expiry, and single consumption.

CREATE TABLE project_close_approvals (
    approval_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    action_json TEXT NOT NULL CHECK(json_valid(action_json)),
    action_digest TEXT NOT NULL,
    verification_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','denied','expired','consumed','revoked')),
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_request_id TEXT,
    consumed_at TEXT,
    consumed_operation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor, request_id),
    CHECK((status='consumed') = (consumed_at IS NOT NULL)),
    CHECK(status NOT IN ('approved','denied','consumed') OR decided_at IS NOT NULL)
);
CREATE UNIQUE INDEX one_live_close_approval ON project_close_approvals(bug_id)
WHERE status IN ('requested','approved');
CREATE TRIGGER close_approval_identity BEFORE UPDATE ON project_close_approvals
WHEN NEW.approval_id IS NOT OLD.approval_id OR NEW.bug_id IS NOT OLD.bug_id
 OR NEW.actor IS NOT OLD.actor OR NEW.request_id IS NOT OLD.request_id
 OR NEW.action_json IS NOT OLD.action_json OR NEW.action_digest IS NOT OLD.action_digest
 OR NEW.verification_digest IS NOT OLD.verification_digest
 OR NEW.requested_at IS NOT OLD.requested_at OR NEW.expires_at IS NOT OLD.expires_at
 OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.status IN ('denied','expired','consumed','revoked') AND NEW.status IS NOT OLD.status)
 OR (OLD.decided_at IS NOT NULL AND (NEW.decided_at IS NOT OLD.decided_at
     OR NEW.decided_by IS NOT OLD.decided_by
     OR NEW.decision_request_id IS NOT OLD.decision_request_id))
 OR (OLD.consumed_at IS NOT NULL AND (NEW.consumed_at IS NOT OLD.consumed_at
     OR NEW.consumed_operation_id IS NOT OLD.consumed_operation_id))
BEGIN SELECT RAISE(ABORT,'close approval identity and terminal decisions are immutable'); END;
CREATE TRIGGER close_approval_retention BEFORE DELETE ON project_close_approvals
BEGIN SELECT RAISE(ABORT,'close approvals require controlled retention'); END;

-- One write attempt per operation. The custody row is inserted before any network
-- mutation; not_issued/precondition_failed prove no mutation command was invoked.
CREATE TABLE project_write_attempts (
    operation_id TEXT PRIMARY KEY REFERENCES project_bug_operations(operation_id),
    write_digest TEXT NOT NULL,
    reader_digest TEXT NOT NULL,
    user_key TEXT NOT NULL,
    baseline_token TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','acknowledged','unknown','precondition_failed','not_issued')),
    response_digest TEXT,
    dispatched_at_ms INTEGER NOT NULL CHECK(dispatched_at_ms >= 0),
    settled_at_ms INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((state='reserved') = (settled_at_ms IS NULL))
);
CREATE TRIGGER write_attempt_transitions BEFORE UPDATE ON project_write_attempts
WHEN OLD.state!='reserved' OR NEW.state NOT IN ('acknowledged','unknown','precondition_failed','not_issued')
 OR NEW.operation_id IS NOT OLD.operation_id OR NEW.write_digest IS NOT OLD.write_digest
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.user_key IS NOT OLD.user_key
 OR NEW.baseline_token IS NOT OLD.baseline_token OR NEW.dispatched_at_ms IS NOT OLD.dispatched_at_ms
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'a write attempt settles a reserved custody row exactly once'); END;
CREATE TRIGGER write_attempt_retention BEFORE DELETE ON project_write_attempts
BEGIN SELECT RAISE(ABORT,'write attempts require controlled retention'); END;

CREATE TABLE project_write_dispatch_requests (
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
CREATE UNIQUE INDEX one_pending_write_dispatch ON project_write_dispatch_requests(operation_id)
WHERE state IN ('queued','running');
CREATE TRIGGER write_dispatch_identity BEFORE UPDATE ON project_write_dispatch_requests
WHEN NEW.dispatch_id IS NOT OLD.dispatch_id OR NEW.operation_id IS NOT OLD.operation_id
 OR NEW.actor IS NOT OLD.actor OR NEW.request_id IS NOT OLD.request_id
 OR NEW.request_digest IS NOT OLD.request_digest OR NEW.runtime_digest IS NOT OLD.runtime_digest
 OR NEW.reader_digest IS NOT OLD.reader_digest OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.state IN ('succeeded','blocked','failed') AND
     (NEW.state IS NOT OLD.state OR NEW.result_json IS NOT OLD.result_json OR NEW.error_code IS NOT OLD.error_code))
BEGIN SELECT RAISE(ABORT,'write dispatch intent and completed result are immutable'); END;
CREATE TRIGGER write_dispatch_retention BEFORE DELETE ON project_write_dispatch_requests
BEGIN SELECT RAISE(ABORT,'write dispatch requires controlled retention'); END;
