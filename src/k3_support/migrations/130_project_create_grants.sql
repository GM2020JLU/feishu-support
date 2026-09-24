-- Creation grants authorize only bounded native Project item creation custody.
CREATE TABLE project_create_grants (
    grant_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    host TEXT NOT NULL,
    project_key TEXT NOT NULL,
    type_key TEXT NOT NULL,
    max_creations INTEGER NOT NULL CHECK(max_creations>=1 AND max_creations<=20),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(actor,request_id)
);
CREATE TRIGGER project_create_grant_identity BEFORE UPDATE OF actor,request_id,request_digest,host,project_key,type_key,max_creations,expires_at,created_at
ON project_create_grants BEGIN
    SELECT RAISE(ABORT,'create grant scope is immutable; issue a new grant');
END;
CREATE TRIGGER project_create_grant_revocation_final
BEFORE UPDATE OF revoked_at ON project_create_grants
WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL BEGIN
    SELECT RAISE(ABORT,'create grant revocation is final');
END;
CREATE TRIGGER project_create_grant_retention BEFORE DELETE ON project_create_grants
BEGIN SELECT RAISE(ABORT,'create grants require controlled retention'); END;
CREATE TABLE project_create_grant_events (
    event_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES project_create_grants(grant_id),
    actor TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('issued','revoked','draft_prepared','creation_settled')),
    created_at TEXT NOT NULL
);
CREATE TRIGGER project_create_grant_event_retention BEFORE DELETE ON project_create_grant_events
BEGIN SELECT RAISE(ABORT,'create grant events require controlled retention'); END;
