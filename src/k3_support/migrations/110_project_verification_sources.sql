CREATE TABLE project_verification_sources (
    run_id TEXT PRIMARY KEY REFERENCES project_verification_runs(run_id),
    bindings_json TEXT NOT NULL CHECK(json_valid(bindings_json)),
    runtime_json TEXT NOT NULL CHECK(json_valid(runtime_json))
);
CREATE TABLE project_verification_observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES project_verification_runs(run_id),
    phase TEXT NOT NULL CHECK(phase IN ('before','after')),
    state TEXT NOT NULL CHECK(state IN ('matched','mismatch','unavailable')),
    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
    recorded_at TEXT NOT NULL,
    UNIQUE(run_id, phase)
);
CREATE TRIGGER project_verification_sources_immutable_update
BEFORE UPDATE ON project_verification_sources BEGIN
    SELECT RAISE(ABORT, 'verification source bindings are immutable');
END;
CREATE TRIGGER project_verification_sources_immutable_delete
BEFORE DELETE ON project_verification_sources BEGIN
    SELECT RAISE(ABORT, 'verification sources require controlled retention');
END;
CREATE TRIGGER project_verification_observations_immutable_update
BEFORE UPDATE ON project_verification_observations BEGIN
    SELECT RAISE(ABORT, 'verification observations are immutable');
END;
CREATE TRIGGER project_verification_observations_immutable_delete
BEFORE DELETE ON project_verification_observations BEGIN
    SELECT RAISE(ABORT, 'verification observations require controlled retention');
END;
