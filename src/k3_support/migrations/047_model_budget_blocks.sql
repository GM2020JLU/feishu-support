CREATE TABLE model_budget_blocks (
    scope_digest TEXT NOT NULL,
    function_name TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('identity_unverified','budget_gate_blocked','ledger_unavailable','model_result_unconfirmed')),
    occurrences INTEGER NOT NULL CHECK(occurrences > 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY(scope_digest,function_name,reason)
);
CREATE INDEX model_budget_blocks_latest ON model_budget_blocks(last_seen_at);
