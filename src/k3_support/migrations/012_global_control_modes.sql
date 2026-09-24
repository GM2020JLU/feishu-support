CREATE TABLE global_control_state (
    scope TEXT PRIMARY KEY CHECK(scope='feishu_support'),
    mode TEXT NOT NULL CHECK(mode IN (
        'observe','collaborate','auto_60','auto','paused','stopped'
    )),
    revision INTEGER NOT NULL CHECK(revision > 0),
    outbound_fence INTEGER NOT NULL CHECK(outbound_fence > 0),
    auto_expires_at TEXT,
    changed_by TEXT NOT NULL,
    change_source TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    CHECK((mode='auto_60' AND auto_expires_at IS NOT NULL)
       OR (mode<>'auto_60' AND auto_expires_at IS NULL))
);

CREATE TABLE global_control_events (
    event_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    external_id TEXT NOT NULL UNIQUE,
    before_mode TEXT,
    after_mode TEXT NOT NULL,
    before_revision INTEGER,
    after_revision INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE global_control_panels (
    panel_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope='feishu_support'),
    operator_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    command_message_id TEXT NOT NULL UNIQUE,
    prompt_message_id TEXT,
    expected_global_revision INTEGER NOT NULL,
    pending_confirmation TEXT CHECK(pending_confirmation IN ('auto','stopped')),
    confirmation_expires_at TEXT,
    state TEXT NOT NULL DEFAULT 'issued' CHECK(state IN ('issued','active','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((pending_confirmation IS NULL AND confirmation_expires_at IS NULL)
       OR (pending_confirmation IS NOT NULL AND confirmation_expires_at IS NOT NULL))
);

CREATE INDEX idx_global_control_panels_active
    ON global_control_panels(scope,chat_id,state,updated_at);

ALTER TABLE outbox ADD COLUMN global_outbound_fence INTEGER NOT NULL DEFAULT 1;
