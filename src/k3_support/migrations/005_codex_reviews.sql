ALTER TABLE jobs ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context_json));

CREATE TABLE codex_reviews (
    review_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id) ON DELETE CASCADE,
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending','verified','rejected','model_failed','waiting_gate','decision_applied')),
    result_digest TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    independent_checks_json TEXT NOT NULL CHECK(json_valid(independent_checks_json)),
    evidence_ids_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_ids_json)),
    hermes_input_digest TEXT,
    hermes_output_json TEXT CHECK(hermes_output_json IS NULL OR json_valid(hermes_output_json)),
    decision_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_codex_reviews_status ON codex_reviews(status, updated_at);
