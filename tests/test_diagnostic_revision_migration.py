from k3_support import db
from k3_support.store import create_case


def test_revision_migration_preserves_legacy_rows(tmp_path, monkeypatch):
    files = [item for item in db.migration_files() if item[0] <= 59]
    monkeypatch.setattr(
        db, "migration_files", lambda: [item for item in files if item[0] <= 58]
    )
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        case_id, _ = create_case(
            conn, title="legacy", case_type="bug", severity="P2", confidence=0.8
        )
        conn.execute(
            """INSERT INTO inbound_events(event_pk,source,identity,external_id,idempotency_key,
               occurred_at,occurred_epoch,received_at,received_epoch,payload_json,status)
            VALUES('legacy','feishu_user_poll','user','legacy','legacy',
               '2026-09-03T00:00:00+00:00',1,'2026-09-03T00:00:00+00:00',1,'{}','processed')"""
        )
        conn.execute(
            "INSERT INTO diagnostic_snapshots VALUES(?,?,?,'{}','[]',0.5,'2026-09-03T00:00:00+00:00',NULL,NULL)",
            ("legacy_snapshot", case_id, "legacy"),
        )
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM diagnostic_snapshots")
        ]
        monkeypatch.setattr(db, "migration_files", lambda: files)
        assert db.migrate(conn) == [59]
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM diagnostic_snapshots")
        ] == before
        conn.execute(
            "INSERT INTO diagnostic_snapshots VALUES(?,?,?,'{}','[]',0.7,'2026-09-03T00:00:01+00:00','new-input','new-source')",
            ("new_snapshot", case_id, "legacy"),
        )
        assert (
            conn.execute("SELECT count(*) FROM diagnostic_snapshots").fetchone()[0] == 2
        )
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.migrate(conn) == []
    finally:
        conn.close()
