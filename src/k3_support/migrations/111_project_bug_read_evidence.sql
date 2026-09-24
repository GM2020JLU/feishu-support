CREATE TABLE project_bug_read_evidence (
    snapshot_id TEXT PRIMARY KEY REFERENCES project_bug_snapshots(snapshot_id),
    actor TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES project_bug_grants(grant_id),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json))
);
CREATE TRIGGER project_bug_read_evidence_immutable_update
BEFORE UPDATE ON project_bug_read_evidence BEGIN
    SELECT RAISE(ABORT, 'Project read evidence is immutable');
END;
CREATE TRIGGER project_bug_read_evidence_immutable_delete
BEFORE DELETE ON project_bug_read_evidence BEGIN
    SELECT RAISE(ABORT, 'Project read evidence requires controlled retention');
END;
