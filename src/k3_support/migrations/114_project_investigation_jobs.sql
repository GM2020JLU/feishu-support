CREATE TABLE project_investigation_jobs (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    round_id TEXT NOT NULL REFERENCES project_bug_rounds(round_id)
);
CREATE INDEX project_investigation_round_jobs ON project_investigation_jobs(round_id);
CREATE TRIGGER project_investigation_binding_immutable
BEFORE UPDATE ON project_investigation_jobs
BEGIN SELECT RAISE(ABORT,'investigation job binding is immutable'); END;
CREATE TRIGGER project_investigation_binding_retention
BEFORE DELETE ON project_investigation_jobs
BEGIN SELECT RAISE(ABORT,'investigation job requires controlled retention'); END;
CREATE TRIGGER project_investigation_binding_check
BEFORE INSERT ON project_investigation_jobs
WHEN NOT EXISTS (
 SELECT 1 FROM jobs j JOIN project_bug_rounds r ON r.round_id=NEW.round_id
 JOIN project_bugs b USING(bug_id)
 WHERE j.job_id=NEW.job_id AND j.case_id=b.case_id AND j.job_type='codex'
 AND r.archived_at IS NULL
)
BEGIN SELECT RAISE(ABORT,'invalid investigation job binding'); END;
CREATE TRIGGER project_investigation_started
AFTER INSERT ON project_investigation_jobs
BEGIN
 UPDATE project_bug_rounds SET execution_state='running'
 WHERE round_id=NEW.round_id;
END;
CREATE TRIGGER project_investigation_progress
AFTER UPDATE OF state ON jobs
WHEN EXISTS(SELECT 1 FROM project_investigation_jobs WHERE job_id=NEW.job_id)
BEGIN
 UPDATE project_bug_rounds SET execution_state=(
  SELECT CASE
   WHEN SUM(j.state IN ('orphaned','unknown'))>0 THEN 'unknown'
   WHEN SUM(j.state IN ('queued','running'))>0 THEN 'running'
   WHEN SUM(j.state='waiting')>0 THEN 'blocked'
   WHEN SUM(j.state='failed')>0 THEN 'failed'
   WHEN SUM(j.state='cancelled')>0 THEN 'cancelled'
   WHEN SUM(j.state='succeeded')=COUNT(*) THEN 'succeeded'
   ELSE 'unknown' END
  FROM project_investigation_jobs b JOIN jobs j USING(job_id)
  WHERE b.round_id=project_bug_rounds.round_id
 ) WHERE round_id=(SELECT round_id FROM project_investigation_jobs WHERE job_id=NEW.job_id)
 AND archived_at IS NULL;
 UPDATE project_bugs SET revision=revision+1 WHERE bug_id=(
 SELECT r.bug_id FROM project_bug_rounds r JOIN project_investigation_jobs b USING(round_id)
 WHERE b.job_id=NEW.job_id);
END;
