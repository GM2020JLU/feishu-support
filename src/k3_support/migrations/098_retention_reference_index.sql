-- Inert until source capture, backfill and independent coverage verification.
CREATE TABLE retention_reference_generations (
    generation INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('building','ready','invalid')),
    schema_digest TEXT NOT NULL,
    coverage_digest TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    shadow_digest TEXT,
    CHECK(state!='ready' OR shadow_digest IS NOT NULL)
);
CREATE TABLE retention_reference_sources (
    source_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    key_spec TEXT NOT NULL,
    kinds TEXT NOT NULL,
    schema_digest TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    backfill_cursor TEXT,
    backfill_complete INTEGER NOT NULL DEFAULT 0 CHECK(backfill_complete IN (0,1)),
    shadow_cursor TEXT,
    shadow_complete INTEGER NOT NULL DEFAULT 0 CHECK(shadow_complete IN (0,1)),
    shadow_error TEXT,
    UNIQUE(table_name,column_name)
);
CREATE TABLE retention_reference_rows (
    source_id TEXT NOT NULL REFERENCES retention_reference_sources(source_id),
    row_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1),
    deleted INTEGER NOT NULL CHECK(deleted IN (0,1)),
    state TEXT NOT NULL CHECK(state IN ('pending','complete','error')),
    value_digest TEXT,
    error_class TEXT,
    shadow_revision INTEGER,
    PRIMARY KEY(source_id,row_key)
);
CREATE TABLE retention_reference_dirty (
    source_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(source_id,row_key),
    FOREIGN KEY(source_id,row_key) REFERENCES retention_reference_rows(source_id,row_key)
);
CREATE TABLE retention_reference_edges (
    source_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    target_digest TEXT NOT NULL,
    PRIMARY KEY(source_id,row_key,target_digest),
    FOREIGN KEY(source_id,row_key) REFERENCES retention_reference_rows(source_id,row_key)
);
CREATE INDEX retention_reference_target ON retention_reference_edges(target_digest);
CREATE INDEX retention_reference_incomplete ON retention_reference_rows(source_id,row_key)
    WHERE state!='complete';
CREATE INDEX retention_reference_unverified ON retention_reference_rows(source_id,row_key)
    WHERE shadow_revision IS NULL OR shadow_revision!=revision;
