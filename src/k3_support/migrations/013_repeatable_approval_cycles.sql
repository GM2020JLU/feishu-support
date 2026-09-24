-- Preserve every terminal approval while allowing the exact same action to be
-- proposed again after denial, expiry, failure, or consumption.  Rebuild the
-- only child table in the same transaction so foreign keys remain enabled.

CREATE TABLE approvals_new (
    approval_id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('board1_lease','wip_push','meeting_create','mail_send','policy_change')),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    session_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','denied','expired','consumed','revoked')),
    requested_action_json TEXT NOT NULL CHECK(json_valid(requested_action_json)),
    action_digest TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    approver_channel TEXT,
    approver_identity TEXT,
    approval_message_id TEXT,
    decision_text TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE meeting_previews_new (
    preview_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    action_json TEXT NOT NULL CHECK(json_valid(action_json)),
    action_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('preview','approved','creating','created','failed','expired','cancelled')),
    approval_id TEXT REFERENCES approvals_new(approval_id),
    calendar_event_id TEXT,
    remote_result_json TEXT CHECK(remote_result_json IS NULL OR json_valid(remote_result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO approvals_new SELECT * FROM approvals;
INSERT INTO meeting_previews_new SELECT * FROM meeting_previews;

DROP TABLE meeting_previews;
DROP TABLE approvals;
ALTER TABLE approvals_new RENAME TO approvals;
ALTER TABLE meeting_previews_new RENAME TO meeting_previews;

CREATE INDEX idx_approvals_gate
ON approvals(case_id, approval_type, status, expires_at);

CREATE UNIQUE INDEX idx_approvals_active_action
ON approvals(approval_type, case_id, action_digest)
WHERE status IN ('requested','approved');

CREATE UNIQUE INDEX idx_meeting_previews_live_action
ON meeting_previews(case_id, action_digest)
WHERE status IN ('preview','approved','creating','created');
