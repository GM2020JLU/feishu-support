CREATE TABLE model_budget_policy (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    currency TEXT NOT NULL,
    daily_limit INTEGER NOT NULL CHECK(daily_limit>0),
    case_limit INTEGER NOT NULL CHECK(case_limit>0),
    attempt_limit INTEGER NOT NULL CHECK(attempt_limit>0),
    revision INTEGER NOT NULL CHECK(revision>0),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE model_budget_attempts (
    attempt_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    case_id TEXT REFERENCES cases(case_id),
    budget_day TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    currency TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    reserved INTEGER NOT NULL CHECK(reserved>0),
    charged INTEGER NOT NULL CHECK(charged>=0),
    state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','unknown','settled','cancelled')),
    receipt_id TEXT UNIQUE,
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX model_budget_daily ON model_budget_attempts(budget_day);
CREATE INDEX model_budget_case ON model_budget_attempts(case_id);
CREATE TABLE model_budget_policy_history (
    revision INTEGER PRIMARY KEY,
    policy_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
