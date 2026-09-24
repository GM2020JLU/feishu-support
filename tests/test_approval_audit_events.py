import sqlite3

import pytest
from test_executors import approved_board, executor_config

from k3_support.audit_inventory import page
from k3_support.store import create_case


def fixture_approval(conn, config):
    case, _ = create_case(
        conn, title="audit", case_type="bug", severity="P2", confidence=0.2
    )
    return approved_board(conn, executor_config(config, board=True), case)


def test_approval_transitions_are_preserved_and_consumption_is_not_attributed_to_approver(
    conn, config
):
    approval = fixture_approval(conn, config)
    rows = conn.execute(
        "SELECT * FROM approval_audit_events WHERE approval_id=? ORDER BY sequence",
        (approval,),
    ).fetchall()
    assert [r["after_status"] for r in rows] == ["requested", "approved"]
    assert rows[0]["decision_actor"] is None and rows[1]["decision_actor"]
    conn.execute(
        "UPDATE approvals SET status='consumed',consumed_at='now' WHERE approval_id=?",
        (approval,),
    )
    latest = conn.execute(
        "SELECT * FROM approval_audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    assert (
        latest["before_status"] == "approved" and latest["after_status"] == "consumed"
    )
    assert latest["decision_actor"] is None
    report = page(conn, kind="approval_history")
    assert report["total_matching"] == 3
    assert "action_digest" not in str(report) and "requested_action_json" not in str(
        report
    )


def test_audit_updates_roll_back_with_operation_and_cannot_be_rewritten(conn, config):
    approval = fixture_approval(conn, config)
    count = conn.execute("SELECT count(*) FROM approval_audit_events").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE approvals SET status='revoked' WHERE approval_id=?", (approval,)
    )
    conn.execute("ROLLBACK")
    assert (
        conn.execute("SELECT count(*) FROM approval_audit_events").fetchone()[0]
        == count
    )
    for sql in [
        "DELETE FROM approval_audit_events",
        "UPDATE approval_audit_events SET after_status='consumed'",
    ]:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


def test_upgrade_preserves_existing_approval_without_inventing_history(
    tmp_path, config, monkeypatch
):
    from k3_support import db

    migrations = db.migration_files()
    monkeypatch.setattr(
        db, "migration_files", lambda: [m for m in migrations if m[0] < 52]
    )
    database = db.connect(tmp_path / "upgrade.sqlite3")
    try:
        db.migrate(database)
        approval = fixture_approval(database, config)
        monkeypatch.setattr(db, "migration_files", lambda: migrations)
        db.migrate(database)
        assert (
            database.execute(
                "SELECT status FROM approvals WHERE approval_id=?", (approval,)
            ).fetchone()[0]
            == "approved"
        )
        assert (
            database.execute("SELECT count(*) FROM approval_audit_events").fetchone()[0]
            == 0
        )
        database.execute(
            "UPDATE approvals SET status='consumed' WHERE approval_id=?", (approval,)
        )
        assert (
            database.execute("SELECT count(*) FROM approval_audit_events").fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_binding_change_is_audited_but_payload_is_not_stored(conn, config):
    approval = fixture_approval(conn, config)
    conn.execute(
        "UPDATE approvals SET requested_action_json='{\"SECRET\":true}' WHERE approval_id=?",
        (approval,),
    )
    row = conn.execute(
        "SELECT * FROM approval_audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    assert row["event_kind"] == "binding_changed"
    assert "SECRET" not in str(dict(row))
    count = conn.execute("SELECT count(*) FROM approval_audit_events").fetchone()[0]
    conn.execute(
        "UPDATE approvals SET updated_at='later' WHERE approval_id=?", (approval,)
    )
    assert (
        conn.execute("SELECT count(*) FROM approval_audit_events").fetchone()[0]
        == count
    )
