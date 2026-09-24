-- Durable custody for native Project bug creation drafts; no transport effects.
CREATE TABLE project_bug_create_drafts (
    draft_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    grant_id TEXT NOT NULL REFERENCES project_create_grants(grant_id),
    host TEXT NOT NULL,
    project_key TEXT NOT NULL,
    type_key TEXT NOT NULL,
    field_values_json TEXT NOT NULL CHECK(json_valid(field_values_json)),
    required_fields_json TEXT NOT NULL CHECK(json_valid(required_fields_json)),
    missing_required_json TEXT NOT NULL CHECK(json_valid(missing_required_json)),
    duplicate_search_id TEXT,
    duplicate_candidates_json TEXT CHECK(duplicate_candidates_json IS NULL OR json_valid(duplicate_candidates_json)),
    duplicate_confirmed_at TEXT,
    state TEXT NOT NULL CHECK(state IN ('draft','ready','dispatched','unknown','created','rejected','cancelled')),
    created_item_id TEXT,
    response_digest TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(actor,request_id),
    CHECK((state='created') = (created_item_id IS NOT NULL)),
    CHECK((state='rejected') = (error_code IS NOT NULL))
);
CREATE UNIQUE INDEX one_inflight_create_per_type ON project_bug_create_drafts(actor,host,project_key,type_key)
WHERE state IN ('dispatched','unknown');
CREATE TRIGGER project_bug_create_draft_identity BEFORE UPDATE ON project_bug_create_drafts
WHEN NEW.draft_id IS NOT OLD.draft_id OR NEW.actor IS NOT OLD.actor
 OR NEW.request_id IS NOT OLD.request_id OR NEW.request_digest IS NOT OLD.request_digest
 OR NEW.grant_id IS NOT OLD.grant_id OR NEW.host IS NOT OLD.host
 OR NEW.project_key IS NOT OLD.project_key OR NEW.type_key IS NOT OLD.type_key
 OR NEW.field_values_json IS NOT OLD.field_values_json
 OR NEW.required_fields_json IS NOT OLD.required_fields_json
 OR NEW.missing_required_json IS NOT OLD.missing_required_json
 OR NEW.created_at IS NOT OLD.created_at
 OR OLD.state IN ('created','rejected','cancelled')
BEGIN SELECT RAISE(ABORT,'create draft identity and terminal result are immutable'); END;
CREATE TRIGGER project_bug_create_draft_retention BEFORE DELETE ON project_bug_create_drafts
BEGIN SELECT RAISE(ABORT,'create drafts require controlled retention'); END;
