CREATE TABLE meeting_dispatch_queue (
    preview_id TEXT PRIMARY KEY REFERENCES meeting_previews(preview_id),
    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id),
    state TEXT NOT NULL CHECK(state IN ('queued','dispatched','finished','needs_review')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_json TEXT
);
CREATE INDEX meeting_dispatch_pending ON meeting_dispatch_queue(created_at,preview_id)
WHERE state='queued';
