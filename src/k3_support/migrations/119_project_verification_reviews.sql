CREATE TABLE project_verification_reviews (
 review_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL REFERENCES project_verification_runs(run_id),
 actor TEXT NOT NULL,
 request_id TEXT NOT NULL,
 request_digest TEXT NOT NULL,
 evidence_digest TEXT NOT NULL,
 verdict TEXT NOT NULL CHECK(verdict IN ('passed','failed','inconclusive')),
 rationale TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(actor,request_id)
);
CREATE TABLE project_verification_dependency_bindings (
 run_id TEXT PRIMARY KEY REFERENCES project_verification_runs(run_id),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json))
);
CREATE TRIGGER verification_dependency_immutable BEFORE UPDATE ON project_verification_dependency_bindings
BEGIN SELECT RAISE(ABORT,'verification dependencies are immutable'); END;
CREATE TRIGGER verification_dependency_retention BEFORE DELETE ON project_verification_dependency_bindings
BEGIN SELECT RAISE(ABORT,'verification dependencies require controlled retention'); END;
CREATE INDEX project_verification_reviews_run ON project_verification_reviews(run_id);
CREATE TRIGGER verification_review_immutable BEFORE UPDATE ON project_verification_reviews
BEGIN SELECT RAISE(ABORT,'verification reviews are immutable'); END;
CREATE TRIGGER verification_review_retention BEFORE DELETE ON project_verification_reviews
BEGIN SELECT RAISE(ABORT,'verification reviews require controlled retention'); END;
