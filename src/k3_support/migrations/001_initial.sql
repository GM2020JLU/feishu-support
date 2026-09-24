CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE inbound_events (
    event_pk TEXT PRIMARY KEY,
    source TEXT NOT NULL CHECK(source IN ('feishu_bot_im','feishu_user_poll','feishu_mail','telegram_control','timer')),
    identity TEXT NOT NULL CHECK(identity IN ('bot','user','telegram_owner','system')),
    external_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    sender_id TEXT,
    chat_id TEXT,
    thread_id TEXT,
    occurred_at TEXT NOT NULL,
    occurred_epoch INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    received_epoch INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    raw_artifact_path TEXT,
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','claimed','processed','ignored','dead_letter')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    UNIQUE(source, identity, external_id)
);

CREATE INDEX idx_inbound_claim ON inbound_events(status, next_attempt_at, received_epoch);

CREATE TABLE cases (
    case_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('faq','investigation','bug','incident','request','mail','meeting')),
    severity TEXT NOT NULL CHECK(severity IN ('P0','P1','P2','P3')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    state TEXT NOT NULL,
    canonical_case_id TEXT REFERENCES cases(case_id),
    requester_id TEXT,
    requester_chat_id TEXT,
    disclosure_class TEXT NOT NULL DEFAULT 'internal' CHECK(disclosure_class IN ('public','internal','team','private','restricted')),
    owner TEXT NOT NULL DEFAULT 'hermes' CHECK(owner IN ('hermes','operator')),
    active_job_id TEXT,
    active_worktree TEXT,
    active_session_id TEXT,
    next_action TEXT,
    last_public_update_at TEXT,
    created_at TEXT NOT NULL,
    created_epoch INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_epoch INTEGER NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0)
);

CREATE INDEX idx_cases_state ON cases(state, severity, updated_epoch);

CREATE TABLE case_events (
    event_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK(actor_type IN ('system','hermes','codex','operator','colleague')),
    actor_id TEXT,
    source_event_pk TEXT REFERENCES inbound_events(event_pk),
    before_state TEXT,
    after_state TEXT,
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    created_epoch INTEGER NOT NULL,
    UNIQUE(case_id, sequence)
);

CREATE TABLE case_sources (
    source_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    stable_external_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    source_version TEXT,
    visibility TEXT NOT NULL,
    requester_access TEXT NOT NULL CHECK(requester_access IN ('allowed','denied','unknown')),
    authority REAL NOT NULL DEFAULT 0 CHECK(authority >= 0 AND authority <= 1),
    updated_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    UNIQUE(case_id, source_type, stable_external_id)
);

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    source_id TEXT REFERENCES case_sources(source_id),
    evidence_layer TEXT NOT NULL CHECK(evidence_layer IN ('static','build','ram_boot','persistent_flash','device_function','stability')),
    freshness_at TEXT NOT NULL,
    visibility TEXT NOT NULL,
    artifact_hash TEXT,
    claim TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    case_id TEXT REFERENCES cases(case_id),
    job_type TEXT NOT NULL CHECK(job_type IN ('retrieve','hermes_case','codex','build','board','push','reply','mail_summary','base_sync','retention')),
    state TEXT NOT NULL CHECK(state IN ('queued','running','waiting','succeeded','failed','cancelled','orphaned')),
    priority INTEGER NOT NULL DEFAULT 100,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    pid INTEGER,
    process_start_token TEXT,
    session_id TEXT,
    workdir TEXT,
    input_digest TEXT NOT NULL,
    output_digest TEXT,
    exit_code INTEGER,
    error_class TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, job_type, input_digest)
);

CREATE INDEX idx_jobs_claim ON jobs(state, available_at, priority, created_at);

CREATE TABLE job_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    worker_id TEXT NOT NULL,
    result TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json)),
    UNIQUE(job_id, attempt_no)
);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('board1_lease','wip_push','meeting_create','mail_send','policy_change')),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    session_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','denied','expired','consumed','revoked')),
    requested_action_json TEXT NOT NULL CHECK(json_valid(requested_action_json)),
    action_digest TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    approver_channel TEXT,
    approver_identity TEXT,
    approval_message_id TEXT,
    decision_text TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(approval_type, case_id, action_digest)
);

CREATE INDEX idx_approvals_gate ON approvals(case_id, approval_type, status, expires_at);

CREATE TABLE outbox (
    outbox_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK(channel IN ('feishu_im','feishu_urgent_app','feishu_urgent_sms','telegram','base','calendar','mail')),
    action_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','delivered','retry','permanent_failure','cancelled')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    remote_message_id TEXT,
    remote_result_json TEXT CHECK(remote_result_json IS NULL OR json_valid(remote_result_json)),
    delivered_at TEXT,
    case_id TEXT REFERENCES cases(case_id),
    source_event_pk TEXT REFERENCES inbound_events(event_pk),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_outbox_claim ON outbox(state, next_attempt_at, created_at);

CREATE TABLE knowledge_entries (
    knowledge_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','approved','stale','retired')),
    question_variants_json TEXT NOT NULL CHECK(json_valid(question_variants_json)),
    answer_markdown TEXT NOT NULL,
    project TEXT,
    module TEXT,
    hardware TEXT,
    software_version TEXT,
    applicability TEXT,
    disclosure_class TEXT NOT NULL CHECK(disclosure_class IN ('public','internal','team','private','restricted')),
    allowed_chat_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(allowed_chat_ids_json)),
    allowed_user_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(allowed_user_ids_json)),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    source_authority REAL NOT NULL CHECK(source_authority >= 0 AND source_authority <= 1),
    evidence_layers_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_layers_json)),
    owner TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_due_at TEXT,
    source_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    canonical_case_id TEXT REFERENCES cases(case_id),
    correction_count INTEGER NOT NULL DEFAULT 0,
    use_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    knowledge_id UNINDEXED,
    title,
    question_variants,
    answer_markdown,
    project,
    module,
    tokenize='unicode61'
);

CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge_entries BEGIN
    INSERT INTO knowledge_fts(knowledge_id,title,question_variants,answer_markdown,project,module)
    VALUES (new.knowledge_id,new.title,new.question_variants_json,new.answer_markdown,coalesce(new.project,''),coalesce(new.module,''));
END;
CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge_entries BEGIN
    DELETE FROM knowledge_fts WHERE knowledge_id=old.knowledge_id;
END;
CREATE TRIGGER knowledge_au AFTER UPDATE ON knowledge_entries BEGIN
    DELETE FROM knowledge_fts WHERE knowledge_id=old.knowledge_id;
    INSERT INTO knowledge_fts(knowledge_id,title,question_variants,answer_markdown,project,module)
    VALUES (new.knowledge_id,new.title,new.question_variants_json,new.answer_markdown,coalesce(new.project,''),coalesce(new.module,''));
END;

CREATE TABLE knowledge_sources (
    mapping_id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL REFERENCES knowledge_entries(knowledge_id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    stable_external_id TEXT NOT NULL,
    url TEXT,
    source_version TEXT,
    visibility TEXT NOT NULL,
    claim TEXT NOT NULL,
    UNIQUE(knowledge_id, source_type, stable_external_id, claim)
);

CREATE TABLE knowledge_feedback (
    feedback_id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL REFERENCES knowledge_entries(knowledge_id) ON DELETE CASCADE,
    case_id TEXT REFERENCES cases(case_id),
    actor_id TEXT,
    verdict TEXT NOT NULL CHECK(verdict IN ('helpful','incorrect','incomplete','sensitive')),
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE watermarks (
    watermark_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    updated_at TEXT NOT NULL
);

CREATE TABLE locks (
    lock_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    case_id TEXT REFERENCES cases(case_id),
    scope TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json))
);

CREATE TABLE health_samples (
    sample_id TEXT PRIMARY KEY,
    component TEXT NOT NULL,
    status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    last_event_at TEXT,
    queue_depth INTEGER,
    lag_seconds REAL,
    error_count INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json))
);

CREATE TABLE base_mappings (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    mirrored_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id),
    UNIQUE(table_id, record_id)
);

CREATE TABLE source_registry (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    stable_external_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    acl_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(acl_json)),
    source_version TEXT,
    content_digest TEXT,
    updated_at TEXT,
    last_checked_at TEXT,
    UNIQUE(source_type, stable_external_id)
);

