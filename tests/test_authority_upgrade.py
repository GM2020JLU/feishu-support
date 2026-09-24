from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_delivery_attempts import active

from k3_support import db
from k3_support.delivery import DeliverySuppressed, claim_outbox, deliver_claimed
from k3_support.operations import reconcile
from k3_support.store import create_case


def test_upgrade_preserves_history_and_quarantines_old_non_idempotent_sending(tmp_path, config, monkeypatch):
    conn = db.connect(tmp_path / "upgrade.db")
    files = db.migration_files()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, "migration_files", lambda: [item for item in files if item[0] <= 23])
            db.migrate(conn)
        case_id, _ = create_case(conn, title="historical answer", case_type="faq", severity="P3", confidence=0.9)
        conn.execute("UPDATE cases SET state='resolved',resolved_at='2026-09-01T00:00:00+00:00' WHERE case_id=?", (case_id,))
        with db.transaction(conn):
            # Historical rows must use the historical schema, not today's writer.
            sending, sent = "old-sending", "old-sent"
            conn.executemany(
                """INSERT INTO outbox(outbox_id,channel,action_type,destination,
                payload_json,idempotency_key,case_id,created_at,updated_at)
                VALUES(?,'telegram','notify','telegram:fixture',?,?,?, ?,?)""",
                [(key, payload, key, case_id, "2026-09-01", "2026-09-01")
                 for key, payload in ((sending, '{"text":"possibly already sent"}'),
                                      (sent, '{"text":"historical"}'))],
            )
        expired = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        conn.execute("UPDATE outbox SET state='sending',lease_owner='old',lease_expires_at=? WHERE outbox_id=?", (expired, sending))
        conn.execute("UPDATE outbox SET state='delivered',remote_message_id='historical-receipt' WHERE outbox_id=?", (sent,))
        snapshot = dict(conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (sending,)).fetchone())
        case_before = dict(conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())
        assert db.migrate(conn) == [version for version, _, _ in files if version > 23]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
        assert {"claim_token", "dispatch_started_at", "effects_finalized_at"} <= columns
        with pytest.raises(DeliverySuppressed, match="claim lost"):
            deliver_claimed(conn, active(config), snapshot, telegram_runner=lambda *_: pytest.fail("old snapshot must not resume"))
        assert reconcile(conn)["uncertain_outbox"] == 1
        assert claim_outbox(conn, worker_id="new-release") is None
        assert conn.execute("SELECT state FROM outbox WHERE outbox_id=?", (sending,)).fetchone()[0] == "permanent_failure"
        assert conn.execute("SELECT remote_message_id FROM outbox WHERE outbox_id=?", (sent,)).fetchone()[0] == "historical-receipt"
        case_after = dict(conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())
        assert {key: case_after[key] for key in case_before} == case_before
        assert case_after["outcome"] == "unknown"
        assert case_after["outcome_provenance"] == "legacy_unknown"
        assert case_after["lifecycle_round"] == 1
        assert db.integrity(conn)["ok"] is True
    finally:
        conn.close()


def test_current_schema_checker_refuses_a_database_beyond_its_known_contract(conn, monkeypatch):
    files = db.migration_files()
    before = [tuple(row) for row in conn.execute("SELECT * FROM schema_migrations")]
    monkeypatch.setattr(db, "migration_files", lambda: files[:-1])
    with pytest.raises(db.DatabaseError, match="newer than this application"):
        db.migrate(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM schema_migrations")] == before
