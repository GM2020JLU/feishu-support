CREATE TABLE mail_digest_runs (
    digest_id TEXT PRIMARY KEY,
    summary_type TEXT NOT NULL CHECK(summary_type IN ('mail_noon','mail_evening')),
    watermark_key TEXT NOT NULL,
    range_start TEXT,
    range_end TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK(item_count >= 0),
    ai_summary_json TEXT NOT NULL CHECK(json_valid(ai_summary_json)),
    content_digest TEXT NOT NULL,
    telegram_destination TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('linking','prepared','delivered','failed')),
    telegram_outbox_id TEXT REFERENCES outbox(outbox_id),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(summary_type, range_end)
);

CREATE TABLE mail_digest_links (
    digest_id TEXT NOT NULL REFERENCES mail_digest_runs(digest_id) ON DELETE CASCADE,
    message_id TEXT NOT NULL REFERENCES mail_items(message_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    share_outbox_id TEXT NOT NULL UNIQUE REFERENCES outbox(outbox_id),
    resolve_outbox_id TEXT UNIQUE REFERENCES outbox(outbox_id),
    im_message_id TEXT,
    message_app_link TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','shared','delivered','failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(digest_id, message_id),
    UNIQUE(digest_id, ordinal)
);

CREATE INDEX idx_mail_digest_state ON mail_digest_runs(state, range_end);
CREATE INDEX idx_mail_digest_links_state ON mail_digest_links(digest_id, state, ordinal);
