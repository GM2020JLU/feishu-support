CREATE TABLE health_alert_state (
    alert_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('active','cleared')),
    fingerprint TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_notified_at TEXT,
    cleared_at TEXT
);
