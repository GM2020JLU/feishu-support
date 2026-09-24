from datetime import UTC, datetime
from uuid import uuid4

from k3_support import db
from k3_support.store import create_case
from k3_support.watch_subscriptions import collect_cases, configure


def test_watch_upgrade_preserves_previous_data_and_can_collect(tmp_path, monkeypatch):
    files = [item for item in db.migration_files() if item[0] <= 60]
    monkeypatch.setattr(db, "migration_files", lambda: [item for item in files if item[0] <= 59])
    conn = db.connect(tmp_path / "previous.db")
    try:
        db.migrate(conn)
        case_id, _ = create_case(conn, title="legacy", case_type="bug", severity="P3", confidence=0.8)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before = {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')] for name in tables}
        monkeypatch.setattr(db, "migration_files", lambda: files)
        assert db.migrate(conn) == [60]
        for name, rows in before.items():
            if name != "schema_migrations":
                assert [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')] == rows
        assert conn.execute("SELECT count(*) FROM watch_subscriptions").fetchone()[0] == 0
        configure(conn, owner_id="owner", source_kind="case", source_key=case_id, enabled=True,
                  expected_revision=0, request_id=str(uuid4()), now=datetime(2020, 1, 1, tzinfo=UTC))
        assert collect_cases(conn, owner_id="owner")["created"] == 1
        assert conn.execute("""SELECT e.case_id FROM watch_actions a
                               JOIN case_events e ON e.event_id=a.source_id""").fetchone()[0] == case_id
        assert db.migrate(conn) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        conn.close()
