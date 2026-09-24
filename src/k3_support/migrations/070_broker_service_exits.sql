CREATE TABLE broker_service_exits (
    grant_id TEXT PRIMARY KEY REFERENCES broker_execution_instances(grant_id),
    invocation_id TEXT NOT NULL,
    main_pid INTEGER NOT NULL CHECK(main_pid>0),
    exit_code INTEGER NOT NULL CHECK(exit_code IN (1,2,3)),
    exit_status INTEGER NOT NULL CHECK(exit_status BETWEEN 0 AND 255),
    observed_at TEXT NOT NULL
);
CREATE TRIGGER broker_service_exits_no_update BEFORE UPDATE ON broker_service_exits BEGIN
    SELECT RAISE(ABORT,'service exit evidence is immutable');
END;
