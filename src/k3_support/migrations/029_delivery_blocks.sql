-- A communication handoff is separate from Case execution ownership/state.
CREATE TABLE delivery_blocks (
    outbox_id TEXT PRIMARY KEY REFERENCES outbox(outbox_id),
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    lifecycle_round INTEGER NOT NULL,
    claim_token TEXT,
    turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
    turn_revision INTEGER NOT NULL,
    communication_fence INTEGER NOT NULL,
    reason TEXT NOT NULL,
    next_action TEXT NOT NULL,
    was_delivered INTEGER NOT NULL CHECK(was_delivered IN (0,1)),
    notification_outbox_id TEXT REFERENCES outbox(outbox_id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_delivery_blocks_case ON delivery_blocks(case_id,lifecycle_round);

CREATE VIEW active_delivery_blocks AS
 SELECT b.* FROM delivery_blocks b
 JOIN cases c ON c.case_id=b.case_id AND c.lifecycle_round=b.lifecycle_round
 JOIN conversation_turns t ON t.turn_id=b.turn_id
   AND t.revision=b.turn_revision AND t.fence=b.communication_fence
 WHERE c.state NOT IN ('resolved','cancelled','takeover','paused')
   AND t.communication_owner='human' AND t.communication_mode='silent'
   AND t.state='human_hold';

-- New authority can authorize a fresh query, never a rejected old answer.
CREATE TRIGGER delivery_block_no_requeue BEFORE UPDATE OF state ON outbox
 WHEN NEW.state IN ('pending','retry','sending')
 AND EXISTS(SELECT 1 FROM delivery_blocks b WHERE b.outbox_id=NEW.outbox_id)
 BEGIN SELECT RAISE(ABORT,'blocked knowledge reply requires a fresh query'); END;
