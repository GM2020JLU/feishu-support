CREATE TABLE mail_meeting_drafts (
    message_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK(revision > 0),
    source_digest TEXT NOT NULL,
    draft_json TEXT NOT NULL CHECK(json_valid(draft_json)),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE mail_meeting_draft_history (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES mail_meeting_drafts(message_id),
    revision INTEGER NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    UNIQUE(message_id, revision)
);
