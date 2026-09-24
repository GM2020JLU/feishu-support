ALTER TABLE mail_digest_runs ADD COLUMN membership_digest TEXT;
ALTER TABLE mail_digest_runs ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai';

CREATE TABLE mail_summary_membership (
    digest_id TEXT NOT NULL REFERENCES mail_digest_runs(digest_id),
    message_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    category TEXT NOT NULL,
    attention TEXT NOT NULL,
    thread_id TEXT,
    received_epoch_ms INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY(digest_id,message_id),
    UNIQUE(message_id),
    UNIQUE(digest_id,ordinal)
);
CREATE INDEX mail_summary_category ON mail_summary_membership(digest_id,category,ordinal);
CREATE TRIGGER mail_summary_membership_immutable BEFORE UPDATE ON mail_summary_membership
BEGIN SELECT RAISE(ABORT,'mail summary membership is immutable'); END;

-- Do not fabricate old membership or re-notify every old message at upgrade.
-- These are explicitly uncertain legacy exclusions, not proof of delivery.
-- Only IDs already present at migration are excluded; later-discovered older
-- messages remain an unassigned backlog, independently of the delivered clock.
CREATE TABLE mail_summary_legacy_exclusions (
    message_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    migrated_at TEXT NOT NULL
);
INSERT INTO mail_summary_legacy_exclusions
 SELECT message_id,'legacy_summary_range_membership_unknown',strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
 FROM mail_items WHERE cast(internal_date AS INTEGER)<=coalesce((
     SELECT max(cast(strftime('%s',range_end) AS INTEGER)*1000
                +cast(substr(strftime('%f',range_end),4,3) AS INTEGER))
     FROM summary_runs WHERE state IN ('prepared','delivered')),0);

CREATE TABLE mail_category_corrections (
    correction_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES mail_catalog_items(message_id),
    category TEXT NOT NULL,
    previous_category TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    external_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL
);
CREATE INDEX mail_category_correction_message ON mail_category_corrections(message_id,created_at);
CREATE TRIGGER mail_category_corrections_immutable BEFORE UPDATE ON mail_category_corrections
BEGIN SELECT RAISE(ABORT,'mail category corrections are immutable'); END;
