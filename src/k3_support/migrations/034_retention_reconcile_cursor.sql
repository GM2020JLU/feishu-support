CREATE TABLE retention_reconcile_cursor (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    last_attempt_id TEXT NOT NULL DEFAULT ''
);
INSERT INTO retention_reconcile_cursor(singleton) VALUES(1);
