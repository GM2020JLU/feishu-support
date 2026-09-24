from datetime import UTC, datetime

import pytest

from k3_support.operations import apply_retention, retention_preview
from k3_support.retention_recovery import reconcile_retention, recover
from k3_support.store import create_case, ingest_event


def candidate(conn, config):
    path = config.data_dir / "attachments" / "old.txt"
    path.parent.mkdir(parents=True)
    path.write_text("retained evidence")
    event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="retention-source",
        payload={"content": "old"},
        occurred_at="2026-01-01T00:00:00+00:00",
        raw_artifact_path=str(path),
    )
    conn.execute(
        "UPDATE inbound_events SET received_at='2026-01-01T00:00:00+00:00' WHERE event_pk=?",
        (event,),
    )
    return (
        path,
        event,
        retention_preview(conn, config, now=datetime(2026, 9, 1, tzinfo=UTC)),
    )


@pytest.mark.parametrize("state", ["triage", "takeover", "investigating"])
def test_new_case_reference_after_preview_holds_file(conn, config, state):
    path, event, preview = candidate(conn, config)
    case, _ = create_case(
        conn,
        title="new reference",
        case_type="bug",
        severity="P3",
        confidence=0.9,
        source_event_pk=event,
    )
    conn.execute("UPDATE cases SET state=? WHERE case_id=?", (state, case))
    result = apply_retention(conn, config, preview)
    assert result["held"] == 1
    assert path.read_text() == "retained evidence"


def test_replacement_after_preview_is_not_removed(conn, config):
    path, _, preview = candidate(conn, config)
    path.unlink()
    path.write_text("new evidence")
    result = apply_retention(conn, config, preview)
    assert result["held"] == 1
    assert path.read_text() == "new evidence"


def test_expiration_changed_after_preview_is_rechecked(conn, config):
    path, event, preview = candidate(conn, config)
    conn.execute(
        "UPDATE inbound_events SET received_at='2099-01-01T00:00:00+00:00' WHERE event_pk=?",
        (event,),
    )
    assert apply_retention(conn, config, preview)["held"] == 1
    assert path.exists()


def test_quarantine_preserves_bytes_and_recovers_idempotently(conn, config):
    path, event, preview = candidate(conn, config)
    assert apply_retention(conn, config, preview)["quarantined"] == 1
    row = conn.execute("SELECT * FROM retention_attempts").fetchone()
    from pathlib import Path

    assert Path(row["quarantine_path"]).read_text() == "retained evidence"
    assert not path.exists()
    assert retention_preview(conn, config) == []
    assert recover(conn, config, row["attempt_id"])["state"] == "restored"
    assert recover(conn, config, row["attempt_id"])["replayed"]
    assert path.read_text() == "retained evidence"
    assert conn.execute(
        "SELECT raw_artifact_path FROM inbound_events WHERE event_pk=?", (event,)
    ).fetchone()[0] == str(path)


def test_database_failure_after_move_keeps_durable_recovery_intent(
    conn, config, monkeypatch
):
    import sqlite3

    import k3_support.retention_recovery as recovery

    path, _, preview = candidate(conn, config)
    actual = recovery.quarantine

    def fail(*args):
        actual(*args)
        raise sqlite3.OperationalError("synthetic post-move DB failure")

    monkeypatch.setattr(recovery, "quarantine", fail)
    assert apply_retention(conn, config, preview)["failed"] == 1
    assert (
        conn.execute("SELECT state FROM retention_attempts").fetchone()[0] == "prepared"
    )
    assert not path.exists()
    result = reconcile_retention(conn, config)
    assert result[0]["state"] == "restored"
    assert path.read_text() == "retained evidence"


def test_new_reference_restores_quarantined_source(conn, config):
    path, event, preview = candidate(conn, config)
    apply_retention(conn, config, preview)
    create_case(
        conn,
        title="need old evidence",
        case_type="bug",
        severity="P3",
        confidence=0.9,
        source_event_pk=event,
    )
    assert reconcile_retention(conn, config)[0]["state"] == "restored"
    assert path.exists()


def test_recovery_never_overwrites_replacement(conn, config):
    from pathlib import Path

    from k3_support.operations import OperationsError

    path, _, preview = candidate(conn, config)
    apply_retention(conn, config, preview)
    row = conn.execute("SELECT * FROM retention_attempts").fetchone()
    path.write_text("new file must survive")
    with pytest.raises(OperationsError, match="occupied"):
        recover(conn, config, row["attempt_id"])
    assert path.read_text() == "new file must survive"
    assert Path(row["quarantine_path"]).read_text() == "retained evidence"


def test_retention_source_parent_symlink_is_rejected(conn, config):
    from k3_support.operations import OperationsError

    path, _, preview = candidate(conn, config)
    parent = path.parent
    moved = parent.with_name("original-attachments")
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OperationsError, match="symlink"):
        apply_retention(conn, config, preview)
    assert (moved / path.name).exists()


def test_late_prepared_snapshot_cannot_cancel_finished_quarantine(
    conn, config, monkeypatch
):
    import k3_support.retention_recovery as recovery

    _, _, preview = candidate(conn, config)
    old_attempt = recovery.prepare(conn, config, preview[0])
    assert apply_retention(conn, config, preview)["quarantined"] == 1
    monkeypatch.setattr(recovery, "prepare", lambda *_: old_attempt)
    assert apply_retention(conn, config, preview)["held"] == 1
    assert (
        conn.execute("SELECT state FROM retention_attempts").fetchone()[0]
        == "quarantined"
    )
    assert recover(conn, config, old_attempt["attempt_id"])["state"] == "restored"


def test_held_prefix_cannot_starve_later_recovery(conn, config):
    import k3_support.retention_recovery as recovery

    path, _, preview = candidate(conn, config)
    attempt = recovery.prepare(conn, config, preview[0])
    conn.execute(
        "UPDATE retention_attempts SET attempt_id='z-last' WHERE attempt_id=?",
        (attempt["attempt_id"],),
    )
    for index in range(100):
        event, _ = ingest_event(
            conn,
            source="feishu_user_poll",
            identity="user",
            external_id=f"held-{index}",
            payload={},
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        conn.execute(
            """INSERT INTO retention_attempts(attempt_id,event_pk,original_path,
                     quarantine_path,file_stamp_json,received_at,state,created_at,updated_at)
                     VALUES(?,?,?,?,'[]','2026-01-01','quarantined','2026-01-01','2026-01-01')""",
            (f"a-{index:03}", event, f"/unused/{index}", f"/unused/q/{index}"),
        )
    assert reconcile_retention(conn, config) == []
    assert reconcile_retention(conn, config) == [
        {"attempt_id": "z-last", "state": "restored", "replayed": False}
    ]
    assert path.exists()
    assert reconcile_retention(conn, config) == []
