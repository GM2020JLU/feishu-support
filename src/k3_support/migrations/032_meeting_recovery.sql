CREATE TABLE meeting_create_attempts (
    attempt_id TEXT PRIMARY KEY,
    preview_id TEXT NOT NULL UNIQUE REFERENCES meeting_previews(preview_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    lifecycle_round INTEGER NOT NULL,
    action_digest TEXT NOT NULL,
    action_json TEXT NOT NULL CHECK(json_valid(action_json)),
    target_json TEXT CHECK(target_json IS NULL OR json_valid(target_json)),
    phase TEXT NOT NULL CHECK(phase IN (
        'prepared','dispatched','uncertain','event_created','inviting','complete',
        'partial','never_dispatched','legacy_uncertain','linked','adopted','conflict')),
    dispatch_token TEXT NOT NULL UNIQUE,
    global_fence INTEGER,
    invitation_dispatched_at TEXT,
    event_id TEXT,
    adopted_target_json TEXT CHECK(adopted_target_json IS NULL OR json_valid(adopted_target_json)),
    successor_preview_id TEXT REFERENCES meeting_previews(preview_id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE meeting_recovery_observations (
    observation_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES meeting_create_attempts(attempt_id),
    kind TEXT NOT NULL CHECK(kind IN ('event_receipt','attendee_receipt','check',
        'error','binding','never_dispatched','successor')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id,kind,payload_digest)
);
CREATE INDEX idx_meeting_attempt_case ON meeting_create_attempts(case_id,phase);
CREATE INDEX idx_meeting_observation_attempt ON meeting_recovery_observations(attempt_id,created_at);

-- There is no dispatch marker, original target or reliable receipt ledger in v1.
-- Never reconstruct those facts from the current default calendar or identity.
INSERT INTO meeting_create_attempts(
    attempt_id,preview_id,case_id,lifecycle_round,action_digest,action_json,
    phase,dispatch_token,created_at,updated_at)
SELECT 'mat_legacy_' || mp.preview_id,mp.preview_id,mp.case_id,
       a.lifecycle_round,mp.action_digest,mp.action_json,'legacy_uncertain',
       'legacy_' || mp.preview_id,mp.created_at,mp.updated_at
FROM meeting_previews mp JOIN approvals a ON a.approval_id=mp.approval_id
WHERE mp.status='creating';

CREATE TRIGGER meeting_observations_no_update BEFORE UPDATE ON meeting_recovery_observations
BEGIN SELECT RAISE(ABORT,'meeting observations are append only'); END;
CREATE TRIGGER meeting_observations_no_delete BEFORE DELETE ON meeting_recovery_observations
BEGIN SELECT RAISE(ABORT,'meeting observations are append only'); END;
CREATE TRIGGER meeting_attempt_identity_immutable BEFORE UPDATE ON meeting_create_attempts
WHEN NEW.preview_id IS NOT OLD.preview_id OR NEW.case_id IS NOT OLD.case_id
 OR NEW.lifecycle_round IS NOT OLD.lifecycle_round OR NEW.action_digest IS NOT OLD.action_digest
 OR NEW.action_json IS NOT OLD.action_json OR NEW.target_json IS NOT OLD.target_json
 OR NEW.dispatch_token IS NOT OLD.dispatch_token
BEGIN SELECT RAISE(ABORT,'meeting attempt identity is immutable'); END;
CREATE TRIGGER meeting_invitation_marker_immutable BEFORE UPDATE ON meeting_create_attempts
WHEN OLD.invitation_dispatched_at IS NOT NULL AND NEW.invitation_dispatched_at IS NOT OLD.invitation_dispatched_at
BEGIN SELECT RAISE(ABORT,'meeting invitation dispatch cannot be reset'); END;
