ALTER TABLE knowledge_entries
ADD COLUMN professional_revision_id TEXT;

CREATE TABLE professional_knowledge_revisions (
    revision_id TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
    knowledge_id TEXT NOT NULL REFERENCES knowledge_entries(knowledge_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN (
        'concept','command_reference','procedure','troubleshooting_tree',
        'compatibility_matrix','known_issue','document_route',
        'validation_recipe','safety_policy'
    )),
    lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN (
        'captured','structured','verified','approved','published',
        'needs_review','retired'
    )),
    revision_digest TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    body_markdown TEXT NOT NULL,
    owner TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    review_due_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(stable_id, revision_number)
);

CREATE INDEX idx_professional_knowledge_current
ON professional_knowledge_revisions(stable_id, lifecycle_state, revision_number DESC);

CREATE TABLE professional_knowledge_claims (
    claim_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES professional_knowledge_revisions(revision_id) ON DELETE CASCADE,
    local_claim_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    risk_class TEXT NOT NULL CHECK(risk_class IN ('read_only','transient','persistent','destructive')),
    required_validation_json TEXT NOT NULL CHECK(json_valid(required_validation_json)),
    UNIQUE(revision_id, local_claim_id)
);

CREATE TABLE professional_claim_sources (
    claim_id TEXT NOT NULL REFERENCES professional_knowledge_claims(claim_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    stable_external_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
    PRIMARY KEY(claim_id, source_id)
);

CREATE TABLE professional_validation_runs (
    validation_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES professional_knowledge_revisions(revision_id) ON DELETE CASCADE,
    claim_id TEXT REFERENCES professional_knowledge_claims(claim_id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK(layer IN (
        'static','build','ram_boot','persistent_flash','device_function','stability'
    )),
    result TEXT NOT NULL CHECK(result IN ('passed','failed','not_applicable')),
    environment_json TEXT NOT NULL CHECK(json_valid(environment_json)),
    artifact_digest TEXT,
    case_id TEXT REFERENCES cases(case_id),
    observed_at TEXT NOT NULL,
    UNIQUE(revision_id, claim_id, layer, observed_at)
);

CREATE TABLE professional_knowledge_publications (
    publication_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE REFERENCES professional_knowledge_revisions(revision_id) ON DELETE CASCADE,
    bundle_digest TEXT NOT NULL,
    approved_bundle_digest TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_knowledge_professional_revision
ON knowledge_entries(professional_revision_id)
WHERE professional_revision_id IS NOT NULL;
