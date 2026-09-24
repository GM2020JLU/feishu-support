-- Remote write uncertainty outlives a worker lease. Never auto-settle history.
ALTER TABLE base_mappings ADD COLUMN base_digest TEXT;
CREATE TABLE base_sync_legacy_holds (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    entity_type TEXT,
    entity_id TEXT,
    state TEXT NOT NULL DEFAULT 'unverified' CHECK(state IN ('unverified','reviewed')),
    review_json TEXT CHECK(review_json IS NULL OR json_valid(review_json))
);
-- Old attempts have no durable transport phase. Require an explicit migration
-- inventory even for apparent success; old late responses could corrupt state.
INSERT INTO base_sync_legacy_holds(job_id,entity_type,entity_id)
SELECT job_id,
    CASE WHEN json_valid(context_json) THEN json_extract(context_json,'$.entity_type') END,
    CASE WHEN json_valid(context_json) THEN json_extract(context_json,'$.entity_id') END
FROM jobs WHERE job_type='base_sync' AND attempt_no>0;
CREATE TABLE base_sync_operations (
    operation_id TEXT PRIMARY KEY,
    base_digest TEXT NOT NULL,
    table_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(job_id),
    attempt_no INTEGER,
    lease_owner TEXT,
    input_digest TEXT,
    lifecycle_round INTEGER,
    state TEXT NOT NULL CHECK(state IN ('reserved','prepared','dispatched','unknown','settled','cancelled')),
    target_version INTEGER,
    fields_json TEXT CHECK(fields_json IS NULL OR json_valid(fields_json)),
    request_digest TEXT,
    write_digest TEXT,
    record_id TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CHECK ((job_id IS NULL AND attempt_no IS NULL AND lease_owner IS NULL AND input_digest IS NULL AND lifecycle_round IS NULL)
        OR (job_id IS NOT NULL AND attempt_no>0 AND lease_owner IS NOT NULL AND input_digest IS NOT NULL AND lifecycle_round>0))
);
CREATE UNIQUE INDEX base_sync_one_unsettled_entity
ON base_sync_operations(base_digest,table_id,entity_type,entity_id)
WHERE state IN ('reserved','prepared','dispatched','unknown');
CREATE INDEX base_sync_attempt_operations ON base_sync_operations(job_id,attempt_no,lease_owner);
CREATE TRIGGER base_sync_operation_identity_immutable
BEFORE UPDATE OF operation_id,base_digest,table_id,entity_type,entity_id,job_id,attempt_no,lease_owner,input_digest,lifecycle_round,created_at
ON base_sync_operations BEGIN SELECT RAISE(ABORT,'Base operation identity is immutable'); END;
CREATE TRIGGER base_sync_operation_state_transition
BEFORE UPDATE OF state ON base_sync_operations
WHEN NOT (NEW.state=OLD.state
    OR (OLD.state='reserved' AND NEW.state IN ('prepared','cancelled'))
    OR (OLD.state='prepared' AND NEW.state IN ('dispatched','settled','cancelled'))
    OR (OLD.state='dispatched' AND NEW.state IN ('unknown','settled'))
    OR (OLD.state='unknown' AND NEW.state='settled'))
BEGIN SELECT RAISE(ABORT,'invalid Base operation transition'); END;
