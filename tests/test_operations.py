from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from k3_support.db import transaction
from k3_support.mail import prepare_summary
from k3_support.operations import (
    OperationsError,
    apply_retention,
    backup_database,
    reconcile,
    restore_probe,
    retention_preview,
)
from k3_support.store import create_case, enqueue_outbox, ingest_event, transition_case


def test_online_backup_can_be_opened_and_restored(config, conn):
    create_case(conn, title="backup", case_type="bug", severity="P2", confidence=0.2)
    result = backup_database(config, now=datetime(2026, 9, 1, 2, 30, tzinfo=UTC))
    assert result["integrity"]["ok"] is True
    assert result["size"] > 0
    before_files = set((config.data_dir / "backups").iterdir())
    probe = restore_probe(result["path"])
    assert probe["counts"]["cases"] == 1
    assert probe["counts"]["case_events"] == 1
    assert set((config.data_dir / "backups").iterdir()) == before_files
    assert len(before_files) == 1


def test_restore_probe_rejects_replaced_backup(config, conn, monkeypatch):
    from pathlib import Path

    from k3_support import operations

    target = Path(backup_database(config)["path"])
    original_integrity = operations.integrity

    def replace_after_read(connection):
        result = original_integrity(connection)
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
        return result

    monkeypatch.setattr(operations, "integrity", replace_after_read)
    with pytest.raises(OperationsError, match="changed"):
        restore_probe(target)


@pytest.mark.parametrize("kind", ["file_link", "parent_link", "wal", "shm", "journal", "missing", "corrupt"])
def test_restore_probe_rejects_unsafe_or_nonstandalone_inputs(config, conn, tmp_path, kind):
    from pathlib import Path

    result = backup_database(config)
    original = Path(result["path"])
    before = original.read_bytes()
    target = original
    if kind == "file_link":
        target = tmp_path / "linked.db"
        target.symlink_to(original)
    elif kind == "parent_link":
        linked = tmp_path / "linked-parent"
        linked.symlink_to(original.parent, target_is_directory=True)
        target = linked / original.name
    elif kind in {"wal", "shm", "journal"}:
        Path(str(original) + "-" + kind).write_bytes(b"unverified sidecar")
    elif kind == "missing":
        target = tmp_path / "missing.db"
    else:
        target = tmp_path / "corrupt.db"
        target.write_bytes(b"not sqlite")
    with pytest.raises(OperationsError):
        restore_probe(target)
    assert original.read_bytes() == before
    if kind == "missing":
        assert not target.exists()


def test_retention_previews_then_deletes_only_inside_allowed_roots(config, conn):
    artifact_dir = config.data_dir / "attachments" / "sha"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "raw.txt"
    artifact.write_text("old payload", encoding="utf-8")
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="old-event",
        payload={"content": "old"},
        occurred_at="2026-06-01T00:00:00+00:00",
        raw_artifact_path=str(artifact),
    )
    conn.execute("UPDATE inbound_events SET received_at='2026-06-01T00:00:00+00:00' WHERE event_pk=?", (event_pk,))
    preview = retention_preview(conn, config, now=datetime(2026, 9, 1, tzinfo=UTC))
    assert len(preview) == 1
    assert {key: preview[0][key] for key in ("path", "event_pk", "status", "bytes")} == {
        "path": str(artifact), "event_pk": event_pk, "status": "delete", "bytes": 11}
    assert preview[0]["file_stamp"]
    result = apply_retention(conn, config, preview)
    assert result["deleted"] == 0 and result["quarantined"] == 1
    assert not artifact.exists()
    assert conn.execute("SELECT status FROM retention_tombstones").fetchone()[0] == "held"


def test_retention_rejects_path_outside_data_roots(config, conn, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="unsafe-event",
        payload={},
        occurred_at="2026-06-01T00:00:00+00:00",
        raw_artifact_path=str(outside),
    )
    conn.execute("UPDATE inbound_events SET received_at='2026-06-01T00:00:00+00:00' WHERE event_pk=?", (event_pk,))
    with pytest.raises(OperationsError, match="escapes"):
        retention_preview(conn, config, now=datetime(2026, 9, 1, tzinfo=UTC))


def test_reconcile_reclaims_expired_inbox_and_outbox(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="stale-event",
        payload={},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    conn.execute(
        "UPDATE inbound_events SET status='claimed',lease_owner='dead',lease_expires_at=? WHERE event_pk=?",
        (old, event_pk),
    )
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_1",
            payload={"text": "x", "identity": "bot"},
            idempotency_key="stale-outbox",
        )
    conn.execute(
        "UPDATE outbox SET state='sending',lease_owner='dead',lease_expires_at=? WHERE outbox_id=?",
        (old, outbox_id),
    )
    result = reconcile(conn)
    assert result["reclaimed_inbox"] == 1
    assert result["reclaimed_outbox"] == 1
    assert conn.execute("SELECT status FROM inbound_events WHERE event_pk=?", (event_pk,)).fetchone()[0] == "new"
    assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()[0] == "retry"


def test_reconcile_retries_only_crash_safe_stale_jobs(conn):
    case_id, _ = create_case(
        conn, title="recovery", case_type="bug", severity="P2", confidence=0.5
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id="test",
        reason="classified",
        expected_version=1,
    )
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    now = datetime.now(UTC).isoformat()
    for job_id, job_type in (
        ("stale-retrieve", "retrieve"),
        ("stale-base", "base_sync"),
        ("stale-codex", "codex"),
        ("stale-push", "push"),
    ):
        job_case_id = None if job_type == "base_sync" else case_id
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,
                   lease_expires_at,input_digest,attempt_no,max_attempts,available_at,
                   created_at,updated_at)
               VALUES(?,?,?,'running','dead-worker',?,?,1,3,?,?,?)""",
            (job_id, job_case_id, job_type, old, job_id, now, now, old),
        )
        conn.execute(
            """INSERT INTO job_attempts(attempt_id,job_id,attempt_no,started_at,worker_id)
               VALUES(?, ?, 1, ?, 'dead-worker')""",
            (f"attempt-{job_id}", job_id, old),
        )

    result = reconcile(conn)

    assert result["recovered_jobs"] == ["stale-base", "stale-retrieve"]
    assert result["orphaned_jobs"] == ["stale-codex", "stale-push"]
    states = {
        row["job_id"]: row["state"]
        for row in conn.execute(
            "SELECT job_id,state FROM jobs WHERE job_id LIKE 'stale-%'"
        )
    }
    assert states == {
        "stale-base": "queued",
        "stale-codex": "orphaned",
        "stale-push": "orphaned",
        "stale-retrieve": "queued",
    }
    attempts = conn.execute(
        "SELECT result,ended_at FROM job_attempts WHERE job_id='stale-retrieve'"
    ).fetchone()
    assert attempts["result"] == "interrupted"
    assert attempts["ended_at"] is not None


def test_reconcile_does_not_retry_safe_job_after_attempt_budget(conn):
    case_id, _ = create_case(
        conn, title="exhausted", case_type="faq", severity="P3", confidence=0.5
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id="test",
        reason="classified",
        expected_version=1,
    )
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,
               input_digest,attempt_no,max_attempts,available_at,created_at,updated_at)
           VALUES('stale-exhausted',?,'retrieve','running','dead-worker',?,'digest',3,3,?,?,?)""",
        (case_id, old, old, old, old),
    )

    result = reconcile(conn)

    assert result["recovered_jobs"] == []
    assert result["orphaned_jobs"] == ["stale-exhausted"]


def test_reconcile_does_not_repeat_uncertain_non_idempotent_delivery(conn):
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    with transaction(conn):
        telegram_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="notify",
            destination="telegram:owner-chat",
            payload={"text": "x"},
            idempotency_key="uncertain-telegram",
        )
    conn.execute(
        "UPDATE outbox SET state='sending',lease_owner='dead',lease_expires_at=? WHERE outbox_id=?",
        (old, telegram_id),
    )
    result = reconcile(conn)
    assert result["uncertain_outbox"] == 1
    row = conn.execute("SELECT state,remote_result_json FROM outbox WHERE outbox_id=?", (telegram_id,)).fetchone()
    assert row["state"] == "permanent_failure"
    assert "not retried" in row["remote_result_json"]


def test_reconcile_converges_sending_row_with_remote_receipt(conn):
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="notify",
            destination="telegram:owner-chat",
            payload={"text": "x"},
            idempotency_key="receipt-before-crash",
        )
    conn.execute(
        "UPDATE outbox SET state='sending',lease_owner='dead',lease_expires_at=?,remote_message_id='tg-1' WHERE outbox_id=?",
        (old, outbox_id),
    )
    result = reconcile(conn)
    assert result["recovered_delivered"] == 1
    assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()[0] == "delivered"


def test_reconcile_repairs_delivered_summary_watermark(conn):
    summary = prepare_summary(
        conn,
        scheduled_at="2026-09-01T12:00:00+08:00",
        destination="telegram:owner-chat",
        slot="mail_noon",
    )
    conn.execute(
        "UPDATE outbox SET state='delivered',remote_message_id='tg-summary' WHERE outbox_id=?",
        (summary["outbox_id"],),
    )

    result = reconcile(conn)

    assert result["recovered_summary_watermarks"] == 1
    assert conn.execute(
        "SELECT state FROM summary_runs WHERE summary_id=?", (summary["summary_id"],)
    ).fetchone()[0] == "delivered"
    assert conn.execute(
        "SELECT 1 FROM watermarks WHERE watermark_key='mail_summary_delivered'"
    ).fetchone()
