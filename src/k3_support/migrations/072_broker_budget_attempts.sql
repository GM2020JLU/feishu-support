CREATE TABLE broker_budget_attempts (
    grant_id TEXT PRIMARY KEY REFERENCES broker_execution_starts(grant_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES model_budget_attempts(attempt_id)
);
