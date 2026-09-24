-- Minimal control-owned resource identity, independent of mutable job context.
CREATE TABLE broker_execution_resources (
    grant_id TEXT PRIMARY KEY REFERENCES broker_execution_starts(grant_id),
    claim_request_id TEXT NOT NULL UNIQUE,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
    lifecycle_round INTEGER NOT NULL CHECK(lifecycle_round > 0),
    input_digest TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    board_session_id TEXT,
    board_not_required INTEGER NOT NULL CHECK(board_not_required IN (0,1)),
    captured_at TEXT NOT NULL,
    settled_at TEXT,
    CHECK ((board_not_required=1 AND board_session_id IS NULL)
        OR (board_not_required=0 AND board_session_id IS NOT NULL AND length(board_session_id)>0))
);
CREATE TRIGGER broker_execution_resources_immutable
BEFORE UPDATE OF grant_id,claim_request_id,job_id,attempt_no,lifecycle_round,
    input_digest,case_id,board_session_id,board_not_required,captured_at
ON broker_execution_resources BEGIN
    SELECT RAISE(ABORT,'execution resource binding is immutable');
END;
CREATE TRIGGER broker_execution_resources_no_reopen
BEFORE UPDATE OF settled_at ON broker_execution_resources
WHEN OLD.settled_at IS NOT NULL BEGIN
    SELECT RAISE(ABORT,'settled execution cannot be reopened');
END;
