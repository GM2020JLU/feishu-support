CREATE TABLE mail_meeting_prepare_cancellations (
    prepare_request_id TEXT PRIMARY KEY REFERENCES mail_meeting_prepare(request_id),
    request_id TEXT NOT NULL UNIQUE,
    binding_digest TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
