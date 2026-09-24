CREATE TABLE broker_execution_contracts (
    grant_id TEXT PRIMARY KEY REFERENCES broker_execution_starts(grant_id),
    contract_digest TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
