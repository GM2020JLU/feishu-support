-- Prospective history only: existing approvals are not backfilled as decisions.
-- No FK: deleting an approval must not erase its audit trail.
CREATE TABLE approval_audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    approval_type TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    before_status TEXT,
    after_status TEXT,
    before_digest TEXT,
    after_digest TEXT,
    decision_actor TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX approval_audit_target ON approval_audit_events(approval_id,observed_at);
CREATE TRIGGER approval_audit_created AFTER INSERT ON approvals BEGIN
    INSERT INTO approval_audit_events VALUES(
        NULL,'aae_'||lower(hex(randomblob(16))),NEW.approval_id,NEW.case_id,NEW.approval_type,
        'created',NULL,NEW.status,NULL,NEW.action_digest,NULL,
        strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
END;
CREATE TRIGGER approval_audit_changed AFTER UPDATE ON approvals
WHEN OLD.status IS NOT NEW.status OR OLD.action_digest IS NOT NEW.action_digest
     OR OLD.requested_action_json IS NOT NEW.requested_action_json
     OR OLD.expires_at IS NOT NEW.expires_at OR OLD.decided_at IS NOT NEW.decided_at
     OR OLD.consumed_at IS NOT NEW.consumed_at OR OLD.approver_identity IS NOT NEW.approver_identity
     OR OLD.approver_channel IS NOT NEW.approver_channel OR OLD.session_id IS NOT NEW.session_id
     OR OLD.case_id IS NOT NEW.case_id
BEGIN
    INSERT INTO approval_audit_events VALUES(
        NULL,'aae_'||lower(hex(randomblob(16))),NEW.approval_id,NEW.case_id,NEW.approval_type,
        CASE WHEN OLD.status IS NOT NEW.status THEN 'status_changed'
             WHEN OLD.action_digest IS NOT NEW.action_digest OR OLD.requested_action_json IS NOT NEW.requested_action_json THEN 'binding_changed'
             ELSE 'metadata_changed' END,
        OLD.status,NEW.status,OLD.action_digest,NEW.action_digest,
        CASE WHEN OLD.decided_at IS NOT NEW.decided_at AND NEW.decided_at IS NOT NULL
                  AND NEW.status IN ('approved','denied') THEN NEW.approver_identity ELSE NULL END,
        strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
END;
CREATE TRIGGER approval_audit_deleted AFTER DELETE ON approvals BEGIN
    INSERT INTO approval_audit_events VALUES(
        NULL,'aae_'||lower(hex(randomblob(16))),OLD.approval_id,OLD.case_id,OLD.approval_type,
        'deleted',OLD.status,NULL,OLD.action_digest,NULL,NULL,
        strftime('%Y-%m-%dT%H:%M:%f+00:00','now'));
END;
CREATE TRIGGER approval_audit_no_update BEFORE UPDATE ON approval_audit_events BEGIN
    SELECT RAISE(ABORT,'approval audit is append-only');
END;
CREATE TRIGGER approval_audit_no_delete BEFORE DELETE ON approval_audit_events BEGIN
    SELECT RAISE(ABORT,'approval audit is append-only');
END;
