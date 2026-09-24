CREATE TABLE project_verification_plans (
    plan_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES project_bug_rounds(round_id),
    version INTEGER NOT NULL CHECK(version > 0),
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
    created_at TEXT NOT NULL,
    UNIQUE(round_id, version),
    UNIQUE(round_id, actor, request_id)
);
CREATE TRIGGER project_verification_plans_immutable_update
BEFORE UPDATE ON project_verification_plans BEGIN
    SELECT RAISE(ABORT, 'verification plans are immutable');
END;
CREATE TRIGGER project_verification_plans_immutable_delete
BEFORE DELETE ON project_verification_plans BEGIN
    SELECT RAISE(ABORT, 'verification plans require controlled retention');
END;
