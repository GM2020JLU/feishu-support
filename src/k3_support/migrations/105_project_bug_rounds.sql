-- Project business state is a remote observation, never a Case/job conclusion.
CREATE TABLE project_bugs (
    bug_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id),
    host TEXT NOT NULL,
    project_key TEXT NOT NULL,
    type_key TEXT NOT NULL,
    item_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    snapshot_sequence INTEGER NOT NULL DEFAULT 0 CHECK(snapshot_sequence >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(host, project_key, type_key, item_id)
);

CREATE TABLE project_bug_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    observation_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(bug_id, sequence),
    UNIQUE(bug_id, observation_id)
);

CREATE TRIGGER project_bug_snapshots_immutable_update
BEFORE UPDATE ON project_bug_snapshots BEGIN
    SELECT RAISE(ABORT, 'project snapshots are immutable');
END;
CREATE TRIGGER project_bug_snapshots_immutable_delete
BEFORE DELETE ON project_bug_snapshots BEGIN
    SELECT RAISE(ABORT, 'project snapshots require controlled retention');
END;

CREATE TABLE project_bug_rounds (
    round_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    number INTEGER NOT NULL CHECK(number > 0),
    reason TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    actor TEXT NOT NULL,
    execution_state TEXT NOT NULL DEFAULT 'planned' CHECK(execution_state IN
        ('planned','running','paused','human','blocked','succeeded','failed','cancelled','unknown')),
    repair_state TEXT NOT NULL DEFAULT 'not_started' CHECK(repair_state IN
        ('not_started','in_progress','ready','not_applicable')),
    verification_state TEXT NOT NULL DEFAULT 'not_run' CHECK(verification_state IN
        ('not_run','running','partial','passed','failed','stale','unknown')),
    archived_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(bug_id, number),
    UNIQUE(bug_id, actor, request_id)
);
CREATE UNIQUE INDEX project_bug_one_open_round ON project_bug_rounds(bug_id)
WHERE archived_at IS NULL;

CREATE TABLE project_bug_events (
    event_id TEXT PRIMARY KEY,
    bug_id TEXT NOT NULL REFERENCES project_bugs(bug_id),
    round_id TEXT REFERENCES project_bug_rounds(round_id),
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    created_at TEXT NOT NULL
);
