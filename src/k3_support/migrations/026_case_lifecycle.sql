-- Delivery and operator-confirmed resolution are separate facts. Legacy closed
-- cases retain their state but do not acquire field-validation provenance.
ALTER TABLE cases ADD COLUMN outcome TEXT NOT NULL DEFAULT 'unknown'
 CHECK(outcome IN ('unknown','answered','awaiting_validation','awaiting_environment_comparison','operator_resolved'));
ALTER TABLE cases ADD COLUMN outcome_provenance TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE cases ADD COLUMN lifecycle_round INTEGER NOT NULL DEFAULT 1 CHECK(lifecycle_round>0);
ALTER TABLE cases ADD COLUMN last_material_progress_at TEXT;
UPDATE cases SET outcome_provenance='legacy_unknown' WHERE state='resolved';
UPDATE cases SET last_material_progress_at=coalesce(
 (SELECT max(created_at) FROM case_events e WHERE e.case_id=cases.case_id
  AND e.event_type IN ('case_created','state_transition','codex_completed','reply_delivered')),
 created_at);

CREATE TABLE case_rounds (
 case_id TEXT NOT NULL REFERENCES cases(case_id),
 round_number INTEGER NOT NULL CHECK(round_number>0),
 started_at TEXT NOT NULL,
 actor_id TEXT,
 reason TEXT NOT NULL,
 initial_case_version INTEGER NOT NULL,
 control_fence INTEGER,
 PRIMARY KEY(case_id,round_number)
);
INSERT INTO case_rounds(case_id,round_number,started_at,reason,initial_case_version)
 SELECT case_id,1,created_at,'legacy or initial authority round',1 FROM cases;

CREATE TABLE case_lifecycle_actions (
 external_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL REFERENCES cases(case_id),
 action TEXT NOT NULL CHECK(action IN ('resolve','reopen')),
 request_digest TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 actor_id TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE case_handoffs (
 review_id TEXT PRIMARY KEY REFERENCES codex_reviews(review_id),
 case_id TEXT NOT NULL REFERENCES cases(case_id),
 lifecycle_round INTEGER NOT NULL,
 content_json TEXT NOT NULL CHECK(json_valid(content_json)),
 created_at TEXT NOT NULL
);

ALTER TABLE jobs ADD COLUMN lifecycle_round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE outbox ADD COLUMN lifecycle_round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE approvals ADD COLUMN lifecycle_round INTEGER NOT NULL DEFAULT 1;
CREATE TRIGGER lifecycle_new_case AFTER INSERT ON cases BEGIN
 UPDATE cases SET last_material_progress_at=NEW.created_at WHERE case_id=NEW.case_id;
 INSERT INTO case_rounds(case_id,round_number,started_at,reason,initial_case_version)
 VALUES(NEW.case_id,1,NEW.created_at,'initial authority round',NEW.version);
END;
CREATE TRIGGER lifecycle_new_job AFTER INSERT ON jobs WHEN NEW.case_id IS NOT NULL BEGIN
 UPDATE jobs SET lifecycle_round=(SELECT lifecycle_round FROM cases WHERE case_id=NEW.case_id)
 WHERE job_id=NEW.job_id;
END;
CREATE TRIGGER lifecycle_new_outbox AFTER INSERT ON outbox WHEN NEW.case_id IS NOT NULL BEGIN
 UPDATE outbox SET lifecycle_round=(SELECT lifecycle_round FROM cases WHERE case_id=NEW.case_id)
 WHERE outbox_id=NEW.outbox_id;
END;
CREATE TRIGGER lifecycle_new_approval AFTER INSERT ON approvals BEGIN
 UPDATE approvals SET lifecycle_round=(SELECT lifecycle_round FROM cases WHERE case_id=NEW.case_id)
 WHERE approval_id=NEW.approval_id;
END;
CREATE TRIGGER lifecycle_old_job BEFORE UPDATE OF state ON jobs
 WHEN NEW.state IN ('queued','running','waiting','succeeded') AND
 NEW.lifecycle_round<>(SELECT lifecycle_round FROM cases WHERE case_id=NEW.case_id)
 BEGIN SELECT RAISE(ABORT,'stale Case round job cannot resume'); END;
CREATE TRIGGER lifecycle_old_outbox BEFORE UPDATE OF state ON outbox
 WHEN NEW.state IN ('pending','retry','sending') AND
 NEW.lifecycle_round<>(SELECT lifecycle_round FROM cases WHERE case_id=NEW.case_id)
 BEGIN SELECT RAISE(ABORT,'stale Case round message cannot resume'); END;
CREATE TRIGGER lifecycle_old_approval BEFORE UPDATE OF status ON approvals
 WHEN NEW.status IN ('requested','approved','consumed') AND
 NEW.lifecycle_round<>(SELECT lifecycle_round FROM cases WHERE case_id=NEW.case_id)
 BEGIN SELECT RAISE(ABORT,'stale Case round approval cannot resume'); END;

-- Heartbeats, reads and Base projection writes affect updated_at only.
CREATE TRIGGER lifecycle_material_case AFTER UPDATE OF state,next_action ON cases
 WHEN NEW.state IS NOT OLD.state OR NEW.next_action IS NOT OLD.next_action BEGIN
 UPDATE cases SET last_material_progress_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
 WHERE case_id=NEW.case_id;
END;
CREATE TRIGGER lifecycle_material_evidence AFTER INSERT ON evidence BEGIN
 UPDATE cases SET last_material_progress_at=NEW.created_at WHERE case_id=NEW.case_id
 AND julianday(NEW.created_at)>=julianday(last_material_progress_at);
END;
