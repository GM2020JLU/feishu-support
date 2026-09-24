CREATE TABLE mail_catalog_runs (
    run_id TEXT PRIMARY KEY,
    folders_json TEXT NOT NULL CHECK(json_valid(folders_json)),
    folder_index INTEGER NOT NULL DEFAULT 0 CHECK(folder_index >= 0),
    next_page_token TEXT,
    pages_processed INTEGER NOT NULL DEFAULT 0 CHECK(pages_processed >= 0),
    messages_seen INTEGER NOT NULL DEFAULT 0 CHECK(messages_seen >= 0),
    state TEXT NOT NULL CHECK(state IN ('running','failed','complete')),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE mail_catalog_items (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    folder TEXT,
    sender_name TEXT,
    sender_address TEXT,
    subject TEXT,
    internal_date TEXT,
    labels_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(labels_json)),
    category TEXT NOT NULL CHECK(category IN (
        'build_ci','code_review','upstream','company','project_release',
        'support_bug','meeting','security_account','external','other'
    )),
    origin TEXT NOT NULL CHECK(origin IN (
        'automation','internal_human','upstream','external','unknown'
    )),
    attention TEXT NOT NULL CHECK(attention IN (
        'action_required','waiting','blocked','information'
    )),
    topics_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(topics_json)),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    classification_source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_mail_catalog_category
ON mail_catalog_items(category, attention, internal_date);
CREATE INDEX idx_mail_catalog_thread ON mail_catalog_items(thread_id, internal_date);
