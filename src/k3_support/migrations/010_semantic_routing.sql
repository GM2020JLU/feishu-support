CREATE TABLE requester_profiles (
    requester_id TEXT PRIMARY KEY,
    relationship TEXT NOT NULL CHECK(relationship IN (
        'supervisor','dotted_supervisor','peer','direct_report','cross_function','external','unknown'
    )),
    function_role TEXT NOT NULL CHECK(function_role IN (
        'engineering','project_manager','product_manager','qa','operations','management','other','unknown'
    )),
    relationship_confidence REAL NOT NULL CHECK(relationship_confidence >= 0 AND relationship_confidence <= 1),
    function_confidence REAL NOT NULL CHECK(function_confidence >= 0 AND function_confidence <= 1),
    source TEXT NOT NULL CHECK(source IN ('operator','feishu_contact','derived','unknown')),
    display_name TEXT,
    department TEXT,
    job_title TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence_json)),
    verified_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE route_decisions (
    route_decision_id TEXT PRIMARY KEY,
    event_pk TEXT NOT NULL UNIQUE REFERENCES inbound_events(event_pk),
    case_id TEXT REFERENCES cases(case_id),
    route TEXT NOT NULL CHECK(route IN (
        'ignore','direct_answer','clarify','research','codex_debug','owner_decision','urgent_notify'
    )),
    proposed_route TEXT NOT NULL CHECK(proposed_route IN (
        'ignore','direct_answer','clarify','research','codex_debug','owner_decision','urgent_notify'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    issue_type TEXT NOT NULL CHECK(issue_type IN (
        'faq','investigation','bug','incident','request','mail','meeting'
    )),
    severity TEXT NOT NULL CHECK(severity IN ('P0','P1','P2','P3')),
    domain TEXT NOT NULL,
    repository_hints_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(repository_hints_json)),
    reason_codes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reason_codes_json)),
    clarification_question TEXT,
    fallback_route TEXT CHECK(fallback_route IS NULL OR fallback_route IN ('research','codex_debug','owner_decision')),
    requires_owner_judgment INTEGER NOT NULL CHECK(requires_owner_judgment IN (0,1)),
    profile_snapshot_json TEXT NOT NULL CHECK(json_valid(profile_snapshot_json)),
    model_output_digest TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'shadow' CHECK(review_status IN ('shadow','accepted','rejected')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_route_decisions_case ON route_decisions(case_id, created_at);
