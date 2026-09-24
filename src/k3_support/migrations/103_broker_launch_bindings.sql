-- Control-owned launch identity: never derive execution selection from worker input.
CREATE TABLE broker_launch_bindings (
    claim_request_id TEXT PRIMARY KEY REFERENCES broker_launches(claim_request_id),
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    input_digest TEXT NOT NULL,
    agent TEXT NOT NULL CHECK(agent IN ('codex','claude','dsh','opencode','hermes')),
    contract_fingerprint TEXT NOT NULL CHECK(length(contract_fingerprint)=64)
);
CREATE TRIGGER broker_launch_bindings_immutable
BEFORE UPDATE ON broker_launch_bindings
BEGIN
    SELECT RAISE(ABORT, 'broker launch binding is immutable');
END;
