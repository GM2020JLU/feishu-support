-- Preserve one authenticated native chat message's exact grant revocation target.
CREATE TABLE project_create_chat_grant_revocations (
    request_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES project_create_grants(grant_id),
    intent_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER project_create_chat_grant_revocation_immutable
BEFORE UPDATE ON project_create_chat_grant_revocations
BEGIN SELECT RAISE(ABORT,'chat create grant revocation is immutable'); END;
CREATE TRIGGER project_create_chat_grant_revocation_retention
BEFORE DELETE ON project_create_chat_grant_revocations
BEGIN SELECT RAISE(ABORT,'chat create grant revocation requires controlled retention'); END;
