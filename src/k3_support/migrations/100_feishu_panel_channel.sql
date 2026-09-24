CREATE TABLE global_control_panels_next (
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
    control_channel TEXT NOT NULL DEFAULT 'telegram' CHECK(control_channel IN ('telegram','gui','feishu')),
    CHECK((pending_confirmation IS NULL AND confirmation_expires_at IS NULL)
       OR (pending_confirmation IS NOT NULL AND confirmation_expires_at IS NOT NULL))
);

INSERT INTO global_control_panels_next(panel_id,scope,operator_user_id,chat_id,command_message_id,prompt_message_id,expected_global_revision,pending_confirmation,confirmation_expires_at,state,created_at,updated_at,control_channel) SELECT panel_id,scope,operator_user_id,chat_id,command_message_id,prompt_message_id,expected_global_revision,pending_confirmation,confirmation_expires_at,state,created_at,updated_at,control_channel FROM global_control_panels;
DROP TABLE global_control_panels;
ALTER TABLE global_control_panels_next RENAME TO global_control_panels;
CREATE INDEX idx_global_control_panels_active ON global_control_panels(scope,chat_id,state,updated_at);
