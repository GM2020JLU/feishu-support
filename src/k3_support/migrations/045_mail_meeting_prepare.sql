CREATE TABLE mail_meeting_prepare (
    request_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES mail_meeting_drafts(message_id),
    draft_revision INTEGER NOT NULL,
    binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
    state TEXT NOT NULL CHECK(state IN ('queued','dispatched','prepared','needs_review')),
    preview_id TEXT REFERENCES meeting_previews(preview_id),
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(message_id,draft_revision)
);
