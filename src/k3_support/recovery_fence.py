"""Revoke historical authority in a newly staged, offline recovery DB only."""
from .db import migration_files, transaction
from .ids import new_id
from .operations import OperationsError
from .timeutil import iso_now


def fence(conn):
    if {row[0] for row in conn.execute("SELECT version FROM schema_migrations")} != {
        version for version, _, _ in migration_files()
    }:
        raise OperationsError("restore fencing requires the matching schema")
    now = iso_now()
    counts = {}
    with transaction(conn):
        previous = conn.execute("SELECT mode,revision FROM global_control_state WHERE scope='feishu_support'").fetchone()
        conn.execute(
            """INSERT INTO global_control_state(scope,mode,revision,outbound_fence,changed_by,change_source,changed_at)
            VALUES('feishu_support','stopped',1,2,'recovery','offline_restore',?)
            ON CONFLICT(scope) DO UPDATE SET mode='stopped',revision=revision+1,
            outbound_fence=outbound_fence+1,auto_expires_at=NULL,changed_by='recovery',
            change_source='offline_restore',changed_at=excluded.changed_at""", (now,))
        conn.execute(
            """INSERT INTO global_control_events VALUES(?, 'feishu_support', ?, ?, 'stopped', ?, ?,
            'recovery','offline_restore','Restored copy quarantined; physical state remains unknown',?)""",
            (new_id("gce"), new_id("restore"), previous[0] if previous else None,
             previous[1] if previous else None, previous[1]+1 if previous else 1, now))
        counts["approvals_revoked"] = conn.execute(
            "UPDATE approvals SET status='revoked',updated_at=? WHERE status IN ('requested','approved')", (now,)).rowcount
        counts["grants_revoked"] = conn.execute(
            "UPDATE broker_grants SET revoked_at=? WHERE revoked_at IS NULL", (now,)).rowcount
        counts['purge_intents_cancelled'] = conn.execute(
            "UPDATE retention_purge_requests SET state='cancelled',updated_at=? WHERE state='prepared'", (now,)).rowcount
        counts['purge_attempts_unknown'] = conn.execute(
            "UPDATE retention_purge_requests SET state='unknown',updated_at=? WHERE state='running'", (now,)).rowcount
        conn.execute("UPDATE global_control_panels SET state='retired',updated_at=? WHERE state<>'retired'", (now,))
        counts["feishu_cards_expired"] = conn.execute(
            "UPDATE feishu_control_cards SET expires_at=? WHERE expires_at>?", (now, now)
        ).rowcount
        counts["jobs_quarantined"] = conn.execute(
            """UPDATE jobs SET state='orphaned',lease_owner=NULL,lease_expires_at=NULL,
            error_class='offline_restore_requires_review',updated_at=?
            WHERE state IN ('queued','running','waiting')""", (now,)).rowcount
        counts["outbox_suppressed"] = conn.execute(
            """UPDATE outbox SET state='cancelled',suppression_reason='offline_restore_unknown_external_effect',
            lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE state IN ('pending','retry','sending')""", (now,)).rowcount
        counts["cases_fenced"] = conn.execute("UPDATE cases SET version=version+1").rowcount
        conn.execute(
            """UPDATE conversation_turns SET fence=fence+1,revision=revision+1,
            communication_owner='human',communication_mode='silent',
            state=CASE WHEN state='closed' THEN state ELSE 'human_hold' END,updated_at=?""", (now,))
        # Locks, process receipts and unknown side effects are evidence, not
        # permissions to be deleted or invented as successfully stopped.
    return {**counts, "mode": "stopped", "physical_state": "unknown", "processes_stopped": False}
