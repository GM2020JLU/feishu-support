-- Old sending rows deliberately retain NULL claim tokens: new code may not
-- resume them from an old in-memory snapshot. Reconcile applies the existing
-- idempotent/unknown-outcome recovery rules before they can be claimed again.
ALTER TABLE outbox ADD COLUMN claim_token TEXT;
ALTER TABLE outbox ADD COLUMN dispatch_started_at TEXT;
ALTER TABLE outbox ADD COLUMN effects_finalized_at TEXT;

CREATE TABLE outbox_attempts (
    claim_token TEXT PRIMARY KEY,
    outbox_id TEXT NOT NULL REFERENCES outbox(outbox_id),
    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
    worker_id TEXT NOT NULL,
    action_digest TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    dispatch_started_at TEXT,
    UNIQUE(outbox_id, attempt_number)
);

CREATE TABLE outbox_attempt_events (
    event_id TEXT PRIMARY KEY,
    claim_token TEXT NOT NULL REFERENCES outbox_attempts(claim_token),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'delivered','failed','uncertain','lease_expired','suppressed'
    )),
    remote_message_id TEXT,
    detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
    recorded_at TEXT NOT NULL,
    UNIQUE(claim_token, event_type)
);
CREATE INDEX idx_outbox_attempts_message ON outbox_attempts(outbox_id);

-- Outcomes are append-only. Late receipts may coexist with an earlier unknown
-- outcome but cannot rewrite that history or the mutable Outbox projection.
CREATE TRIGGER immutable_outbox_attempt_event
BEFORE UPDATE ON outbox_attempt_events
BEGIN
    SELECT RAISE(ABORT, 'outbox attempt events are immutable');
END;

CREATE TRIGGER immutable_outbox_attempt_identity
BEFORE UPDATE ON outbox_attempts
WHEN NEW.claim_token <> OLD.claim_token
  OR NEW.outbox_id <> OLD.outbox_id
  OR NEW.attempt_number <> OLD.attempt_number
  OR NEW.worker_id <> OLD.worker_id
  OR NEW.action_digest <> OLD.action_digest
  OR NEW.claimed_at <> OLD.claimed_at
  OR (OLD.dispatch_started_at IS NOT NULL
      AND NEW.dispatch_started_at IS NOT OLD.dispatch_started_at)
BEGIN
    SELECT RAISE(ABORT, 'outbox attempt identity is immutable');
END;
