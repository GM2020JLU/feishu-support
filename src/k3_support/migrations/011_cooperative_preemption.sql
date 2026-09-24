CREATE TABLE conversation_turns (
    turn_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    source_event_pk TEXT NOT NULL UNIQUE REFERENCES inbound_events(event_pk),
    source_message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL CHECK(chat_type IN ('p2p','group')),
    thread_id TEXT,
    root_message_id TEXT,
    reply_to_message_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    communication_owner TEXT NOT NULL DEFAULT 'ai' CHECK(communication_owner IN ('ai','human')),
    communication_mode TEXT NOT NULL DEFAULT 'respond' CHECK(communication_mode IN ('respond','suggest_only','silent')),
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN (
        'open','ai_scheduled','ai_sending','ai_sent','human_hold','human_answered','closed'
    )),
    fence INTEGER NOT NULL DEFAULT 1 CHECK(fence > 0),
    human_activity_at TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_conversation_turns_case
    ON conversation_turns(case_id, state, created_at);
CREATE INDEX idx_conversation_turns_chat
    ON conversation_turns(chat_id, state, created_at);
CREATE INDEX idx_conversation_turns_source_message
    ON conversation_turns(source_message_id);

CREATE TABLE operator_activities (
    activity_id TEXT PRIMARY KEY,
    external_id TEXT NOT NULL UNIQUE,
    activity_type TEXT NOT NULL CHECK(activity_type IN ('message','reaction','telegram_control')),
    signal TEXT NOT NULL CHECK(signal IN ('hard','soft','explicit')),
    action TEXT NOT NULL CHECK(action IN (
        'claim','human_answered','suggest_only','delegate','full_takeover','pause','details'
    )),
    message_id TEXT,
    chat_id TEXT,
    thread_id TEXT,
    root_message_id TEXT,
    reply_to_message_id TEXT,
    matched_turn_id TEXT REFERENCES conversation_turns(turn_id),
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json)),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_operator_activities_turn
    ON operator_activities(matched_turn_id, occurred_at);

ALTER TABLE outbox ADD COLUMN turn_id TEXT REFERENCES conversation_turns(turn_id);
ALTER TABLE outbox ADD COLUMN turn_revision INTEGER;
ALTER TABLE outbox ADD COLUMN communication_fence INTEGER;
ALTER TABLE outbox ADD COLUMN not_before TEXT;
ALTER TABLE outbox ADD COLUMN suppression_reason TEXT;

CREATE INDEX idx_outbox_coordination
    ON outbox(state, not_before, turn_id, communication_fence);
