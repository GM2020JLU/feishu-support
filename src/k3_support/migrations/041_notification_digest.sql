CREATE TABLE notification_digest_batches (
    digest_outbox_id TEXT PRIMARY KEY REFERENCES outbox(outbox_id),
    summary_digest TEXT NOT NULL
);
CREATE TABLE notification_digest_members (
    digest_outbox_id TEXT NOT NULL REFERENCES outbox(outbox_id),
    original_outbox_id TEXT NOT NULL REFERENCES outbox(outbox_id),
    original_digest TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    PRIMARY KEY(digest_outbox_id,original_outbox_id)
);
CREATE UNIQUE INDEX notification_digest_active_original ON notification_digest_members(original_outbox_id) WHERE active=1;
