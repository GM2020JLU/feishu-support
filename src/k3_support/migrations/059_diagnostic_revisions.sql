CREATE TABLE diagnostic_snapshots_revised (
    snapshot_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    event_pk TEXT NOT NULL REFERENCES inbound_events(event_pk),
    facts_json TEXT NOT NULL CHECK(json_valid(facts_json)),
    missing_json TEXT NOT NULL CHECK(json_valid(missing_json)),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    input_digest TEXT,
    source_digest TEXT,
    UNIQUE(event_pk,input_digest,source_digest)
);
INSERT INTO diagnostic_snapshots_revised SELECT snapshot_id,case_id,event_pk,facts_json,missing_json,confidence,created_at,input_digest,source_digest FROM diagnostic_snapshots;
DROP TABLE diagnostic_snapshots;
ALTER TABLE diagnostic_snapshots_revised RENAME TO diagnostic_snapshots;
CREATE INDEX idx_diagnostic_case ON diagnostic_snapshots(case_id,created_at);
CREATE INDEX idx_diagnostic_event ON diagnostic_snapshots(event_pk);
