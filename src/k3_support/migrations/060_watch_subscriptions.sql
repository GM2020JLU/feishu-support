CREATE TABLE watch_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('release','case')),
    source_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id,source_kind,source_key)
);
CREATE TABLE watch_subscription_history (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json))
);
CREATE TABLE watch_actions (
    action_id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES watch_subscriptions(subscription_id),
    source_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(subscription_id,source_id)
);
