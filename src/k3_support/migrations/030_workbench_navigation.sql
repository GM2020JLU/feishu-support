-- Permanent business addresses, not browsing snapshots. Source deletion never
-- recycles an address. Restore must rotate workbench_navigation_namespace.
CREATE TABLE workbench_item_keys (
    item_seq INTEGER PRIMARY KEY AUTOINCREMENT CHECK(item_seq BETWEEN 1 AND 281474976710655),
    entity_kind TEXT NOT NULL CHECK(entity_kind IN ('case','approval','knowledge','outbox','job','mail')),
    target_key TEXT NOT NULL,
    UNIQUE(entity_kind,target_key)
);
CREATE TABLE workbench_navigation_namespace (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    instance_id BLOB NOT NULL CHECK(typeof(instance_id)='blob' AND length(instance_id)=16),
    legacy_invalid_timestamps INTEGER NOT NULL DEFAULT 0 CHECK(legacy_invalid_timestamps>=0)
);
INSERT INTO workbench_navigation_namespace(singleton,instance_id) VALUES(1,randomblob(16));
UPDATE workbench_navigation_namespace SET legacy_invalid_timestamps=(
    SELECT count(*) FROM (
        SELECT created_at FROM cases UNION ALL SELECT created_at FROM approvals
        UNION ALL SELECT created_at FROM knowledge_entries UNION ALL SELECT created_at FROM outbox
        UNION ALL SELECT created_at FROM jobs UNION ALL SELECT created_at FROM mail_digest_links
    ) WHERE julianday(created_at) IS NULL
);
-- Invalid legacy timestamps sort first, with deterministic kind/key tie breaks.
-- Ordering is registration order from here onward, never mutable priority.
INSERT INTO workbench_item_keys(entity_kind,target_key)
SELECT entity_kind,target_key FROM (
    SELECT 'case' AS entity_kind,case_id AS target_key,created_at FROM cases
    UNION ALL
    SELECT 'approval' AS entity_kind,approval_id AS target_key,created_at FROM approvals
    UNION ALL
    SELECT 'knowledge' AS entity_kind,knowledge_id AS target_key,created_at FROM knowledge_entries
    UNION ALL
    SELECT 'outbox' AS entity_kind,outbox_id AS target_key,created_at FROM outbox
    UNION ALL
    SELECT 'job' AS entity_kind,job_id AS target_key,created_at FROM jobs
    UNION ALL
    SELECT 'mail' AS entity_kind,json_array(digest_id,message_id) AS target_key,created_at FROM mail_digest_links
) ORDER BY julianday(created_at),entity_kind,target_key;
CREATE TRIGGER workbench_keys_no_update BEFORE UPDATE ON workbench_item_keys
BEGIN SELECT RAISE(ABORT,'workbench identity is immutable'); END;
CREATE TRIGGER workbench_keys_no_delete BEFORE DELETE ON workbench_item_keys
BEGIN SELECT RAISE(ABORT,'workbench identity is immutable'); END;
CREATE TRIGGER workbench_keys_exhausted BEFORE INSERT ON workbench_item_keys
WHEN NEW.item_seq=-1 AND (SELECT seq FROM sqlite_sequence WHERE name='workbench_item_keys')>=281474976710655
BEGIN SELECT RAISE(ABORT,'workbench identity space exhausted'); END;

CREATE TRIGGER workbench_case_identity BEFORE UPDATE OF case_id ON cases
WHEN NEW.case_id IS NOT OLD.case_id
BEGIN SELECT RAISE(ABORT,'workbench source identity is immutable'); END;
CREATE TRIGGER workbench_case_no_reuse BEFORE INSERT ON cases
WHEN EXISTS(SELECT 1 FROM workbench_item_keys WHERE entity_kind='case' AND target_key=NEW.case_id)
 AND NOT EXISTS(SELECT 1 FROM cases WHERE case_id=NEW.case_id)
BEGIN SELECT RAISE(ABORT,'deleted workbench identity cannot be reused'); END;
CREATE TRIGGER workbench_case_register AFTER INSERT ON cases
BEGIN
    INSERT OR IGNORE INTO workbench_item_keys(entity_kind,target_key) VALUES('case',NEW.case_id);
END;

CREATE TRIGGER workbench_approval_identity BEFORE UPDATE OF approval_id ON approvals
WHEN NEW.approval_id IS NOT OLD.approval_id
BEGIN SELECT RAISE(ABORT,'workbench source identity is immutable'); END;
CREATE TRIGGER workbench_approval_no_reuse BEFORE INSERT ON approvals
WHEN EXISTS(SELECT 1 FROM workbench_item_keys WHERE entity_kind='approval' AND target_key=NEW.approval_id)
 AND NOT EXISTS(SELECT 1 FROM approvals WHERE approval_id=NEW.approval_id)
BEGIN SELECT RAISE(ABORT,'deleted workbench identity cannot be reused'); END;
CREATE TRIGGER workbench_approval_register AFTER INSERT ON approvals
BEGIN
    INSERT OR IGNORE INTO workbench_item_keys(entity_kind,target_key) VALUES('approval',NEW.approval_id);
END;

CREATE TRIGGER workbench_knowledge_identity BEFORE UPDATE OF knowledge_id ON knowledge_entries
WHEN NEW.knowledge_id IS NOT OLD.knowledge_id
BEGIN SELECT RAISE(ABORT,'workbench source identity is immutable'); END;
CREATE TRIGGER workbench_knowledge_no_reuse BEFORE INSERT ON knowledge_entries
WHEN EXISTS(SELECT 1 FROM workbench_item_keys WHERE entity_kind='knowledge' AND target_key=NEW.knowledge_id)
 AND NOT EXISTS(SELECT 1 FROM knowledge_entries WHERE knowledge_id=NEW.knowledge_id)
BEGIN SELECT RAISE(ABORT,'deleted workbench identity cannot be reused'); END;
CREATE TRIGGER workbench_knowledge_register AFTER INSERT ON knowledge_entries
BEGIN
    INSERT OR IGNORE INTO workbench_item_keys(entity_kind,target_key) VALUES('knowledge',NEW.knowledge_id);
END;

CREATE TRIGGER workbench_outbox_identity BEFORE UPDATE OF outbox_id ON outbox
WHEN NEW.outbox_id IS NOT OLD.outbox_id
BEGIN SELECT RAISE(ABORT,'workbench source identity is immutable'); END;
CREATE TRIGGER workbench_outbox_no_reuse BEFORE INSERT ON outbox
WHEN EXISTS(SELECT 1 FROM workbench_item_keys WHERE entity_kind='outbox' AND target_key=NEW.outbox_id)
 AND NOT EXISTS(SELECT 1 FROM outbox WHERE outbox_id=NEW.outbox_id)
BEGIN SELECT RAISE(ABORT,'deleted workbench identity cannot be reused'); END;
CREATE TRIGGER workbench_outbox_register AFTER INSERT ON outbox
BEGIN
    INSERT OR IGNORE INTO workbench_item_keys(entity_kind,target_key) VALUES('outbox',NEW.outbox_id);
END;

CREATE TRIGGER workbench_job_identity BEFORE UPDATE OF job_id ON jobs
WHEN NEW.job_id IS NOT OLD.job_id
BEGIN SELECT RAISE(ABORT,'workbench source identity is immutable'); END;
CREATE TRIGGER workbench_job_no_reuse BEFORE INSERT ON jobs
WHEN EXISTS(SELECT 1 FROM workbench_item_keys WHERE entity_kind='job' AND target_key=NEW.job_id)
 AND NOT EXISTS(SELECT 1 FROM jobs WHERE job_id=NEW.job_id)
BEGIN SELECT RAISE(ABORT,'deleted workbench identity cannot be reused'); END;
CREATE TRIGGER workbench_job_register AFTER INSERT ON jobs
BEGIN
    INSERT OR IGNORE INTO workbench_item_keys(entity_kind,target_key) VALUES('job',NEW.job_id);
END;

CREATE TRIGGER workbench_mail_identity BEFORE UPDATE OF digest_id,message_id ON mail_digest_links
WHEN NEW.digest_id IS NOT OLD.digest_id OR NEW.message_id IS NOT OLD.message_id
BEGIN SELECT RAISE(ABORT,'workbench source identity is immutable'); END;
CREATE TRIGGER workbench_mail_no_reuse BEFORE INSERT ON mail_digest_links
WHEN EXISTS(SELECT 1 FROM workbench_item_keys WHERE entity_kind='mail' AND target_key=json_array(NEW.digest_id,NEW.message_id))
 AND NOT EXISTS(SELECT 1 FROM mail_digest_links WHERE digest_id=NEW.digest_id AND message_id=NEW.message_id)
BEGIN SELECT RAISE(ABORT,'deleted workbench identity cannot be reused'); END;
CREATE TRIGGER workbench_mail_register AFTER INSERT ON mail_digest_links
BEGIN
    INSERT OR IGNORE INTO workbench_item_keys(entity_kind,target_key) VALUES('mail',json_array(NEW.digest_id,NEW.message_id));
END;
