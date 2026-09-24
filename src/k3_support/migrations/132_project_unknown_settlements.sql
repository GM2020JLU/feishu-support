CREATE TABLE project_unknown_settlements (
    settlement_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES project_bug_operations(operation_id),
    actor TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('confirmed_applied','confirmed_not_applied')),
    evidence_text TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER project_unknown_settlement_immutable
BEFORE UPDATE ON project_unknown_settlements
BEGIN SELECT RAISE(ABORT,'unknown settlement is immutable'); END;
CREATE TRIGGER project_unknown_settlement_retention
BEFORE DELETE ON project_unknown_settlements
BEGIN SELECT RAISE(ABORT,'unknown settlements require controlled retention'); END;
