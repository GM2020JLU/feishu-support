CREATE TABLE retention_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_hash TEXT,
    bytes_removed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('deleted','held','failed','missing')),
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_type, artifact_path, created_at)
);

CREATE TABLE service_state (
    component TEXT PRIMARY KEY,
    pid INTEGER,
    started_at TEXT,
    heartbeat_at TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json))
);

