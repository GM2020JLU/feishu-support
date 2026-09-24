CREATE TABLE attention_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    category TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    snooze_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id,category)
);
CREATE TABLE attention_actions (
    action_id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES attention_subscriptions(subscription_id),
    message_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subscription_id,message_id)
);
CREATE TABLE attention_subscription_history (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json))
);
