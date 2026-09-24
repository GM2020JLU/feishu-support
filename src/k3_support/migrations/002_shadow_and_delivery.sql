CREATE TABLE case_suggestions (
    suggestion_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('classification','knowledge_answer','reply_draft','mail_classification','next_action')),
    content_json TEXT NOT NULL CHECK(json_valid(content_json)),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids_json)),
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'shadow' CHECK(status IN ('shadow','accepted','rejected','expired')),
    created_at TEXT NOT NULL,
    UNIQUE(case_id, kind, policy_version, content_json)
);

CREATE TABLE action_ledger (
    action_key TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    case_id TEXT REFERENCES cases(case_id),
    state TEXT NOT NULL CHECK(state IN ('prepared','started','verified','failed','uncertain','cancelled')),
    input_digest TEXT NOT NULL,
    remote_id TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE mail_items (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    mailbox TEXT NOT NULL DEFAULT 'me',
    sender_name TEXT,
    sender_address TEXT,
    subject TEXT,
    body_preview TEXT,
    folder_id TEXT,
    label_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(label_ids_json)),
    internal_date TEXT,
    classification TEXT CHECK(classification IS NULL OR classification IN ('urgent_action','action','waiting','information','noise')),
    classification_reason TEXT,
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    deadline TEXT,
    requested_action TEXT,
    notified INTEGER NOT NULL DEFAULT 0 CHECK(notified IN (0,1)),
    case_id TEXT REFERENCES cases(case_id),
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_mail_summary ON mail_items(internal_date, classification, notified);

