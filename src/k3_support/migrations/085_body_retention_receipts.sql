CREATE TABLE body_retention_receipts (
    event_pk TEXT PRIMARY KEY REFERENCES inbound_events(event_pk),
    preview_digest TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    body_bytes INTEGER NOT NULL,
    retention_days INTEGER NOT NULL,
    actor TEXT NOT NULL,
    cleared_at TEXT NOT NULL
);
