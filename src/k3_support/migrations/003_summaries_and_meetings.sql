CREATE TABLE summary_runs (
    summary_id TEXT PRIMARY KEY,
    summary_type TEXT NOT NULL CHECK(summary_type IN ('mail_noon','mail_evening','knowledge_review','health')),
    watermark_key TEXT NOT NULL,
    range_start TEXT,
    range_end TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    outbox_id TEXT NOT NULL REFERENCES outbox(outbox_id),
    state TEXT NOT NULL CHECK(state IN ('prepared','delivered','failed')),
    remote_message_id TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(summary_type, range_end)
);

CREATE TABLE meeting_previews (
    preview_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    action_json TEXT NOT NULL CHECK(json_valid(action_json)),
    action_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('preview','approved','creating','created','failed','expired','cancelled')),
    approval_id TEXT REFERENCES approvals(approval_id),
    calendar_event_id TEXT,
    remote_result_json TEXT CHECK(remote_result_json IS NULL OR json_valid(remote_result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, action_digest)
);

