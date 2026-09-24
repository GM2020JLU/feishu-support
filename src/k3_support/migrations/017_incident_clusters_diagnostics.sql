CREATE TABLE incident_clusters (
    cluster_id TEXT PRIMARY KEY,
    canonical_case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id),
    state TEXT NOT NULL CHECK(state IN ('open','closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE incident_cluster_members (
    cluster_id TEXT NOT NULL REFERENCES incident_clusters(cluster_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id) ON DELETE CASCADE,
    similarity REAL NOT NULL CHECK(similarity >= 0 AND similarity <= 1),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(cluster_id, case_id)
);

CREATE TABLE diagnostic_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    event_pk TEXT NOT NULL UNIQUE REFERENCES inbound_events(event_pk),
    facts_json TEXT NOT NULL CHECK(json_valid(facts_json)),
    missing_json TEXT NOT NULL CHECK(json_valid(missing_json)),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_incident_cluster_members ON incident_cluster_members(cluster_id, created_at);
CREATE INDEX idx_diagnostic_case ON diagnostic_snapshots(case_id, created_at);
