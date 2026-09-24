CREATE TABLE mail_action_state (
    message_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK(revision > 0),
    state TEXT NOT NULL CHECK(state IN ('todo','done','snoozed')),
    snooze_until TEXT,
    linked_case_id TEXT REFERENCES cases(case_id),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((state = 'snoozed') = (snooze_until IS NOT NULL))
);
CREATE TABLE mail_action_history (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES mail_action_state(message_id),
    revision INTEGER NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    UNIQUE(message_id, revision)
);
