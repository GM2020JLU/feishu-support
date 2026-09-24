CREATE TABLE feishu_control_cards (
    card_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    request_message_id TEXT NOT NULL,
    delivered_message_id TEXT,
    commands_json TEXT NOT NULL CHECK(json_valid(commands_json)),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
