CREATE TABLE project_repair_reviews (
 review_id TEXT PRIMARY KEY,
 round_id TEXT NOT NULL REFERENCES project_bug_rounds(round_id),
 actor TEXT NOT NULL,
 request_id TEXT NOT NULL,
 request_digest TEXT NOT NULL,
 evidence_digest TEXT NOT NULL,
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 verdict TEXT NOT NULL CHECK(verdict IN ('ready','in_progress','not_applicable')),
 rationale TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(actor,request_id)
);
CREATE INDEX project_repair_reviews_round ON project_repair_reviews(round_id);
CREATE TRIGGER project_repair_reviews_immutable BEFORE UPDATE ON project_repair_reviews
BEGIN SELECT RAISE(ABORT,'repair reviews are immutable'); END;
CREATE TRIGGER project_repair_reviews_retention BEFORE DELETE ON project_repair_reviews
BEGIN SELECT RAISE(ABORT,'repair reviews require controlled retention'); END;
