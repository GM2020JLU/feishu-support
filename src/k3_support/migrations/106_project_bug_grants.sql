-- Scope grants extend control-plane authority; workers cannot write this DB.
CREATE TABLE project_bug_grants (
    grant_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(actor, request_id)
);
CREATE TRIGGER project_bug_grant_scope_immutable
BEFORE UPDATE OF actor,request_id,request_digest,scope_json,expires_at,created_at
ON project_bug_grants BEGIN
    SELECT RAISE(ABORT, 'grant scope is immutable; issue a new grant');
END;
CREATE TRIGGER project_bug_grant_revocation_final
BEFORE UPDATE OF revoked_at ON project_bug_grants
WHEN OLD.revoked_at IS NOT NULL BEGIN
    SELECT RAISE(ABORT, 'grant revocation is final');
END;
CREATE TABLE project_bug_grant_events (
    event_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES project_bug_grants(grant_id),
    actor TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('issued','revoked')),
    created_at TEXT NOT NULL
);
