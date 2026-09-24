from test_watch_races import seed

from k3_support import db
from k3_support.watch_subscriptions import mark_seen, page


def test_seen_upgrade_preserves_collected_actions(tmp_path, monkeypatch):
    files = [item for item in db.migration_files() if item[0] <= 61]
    monkeypatch.setattr(db, "migration_files", lambda: [item for item in files if item[0] <= 60])
    conn = db.connect(tmp_path / "previous.db")
    try:
        db.migrate(conn)
        _, collect = seed(conn, "release")
        collect(conn)
        before = {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
                  for name in ("watch_actions", "watch_subscriptions", "watch_subscription_history", "release_impacts")}
        monkeypatch.setattr(db, "migration_files", lambda: files)
        assert db.migrate(conn) == [61]
        for name, rows in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")] == rows
        assert conn.execute("SELECT count(*) FROM watch_seen").fetchone()[0] == 0
        action = page(conn, owner_id="owner")["items"][0]["action_id"]
        mark_seen(conn, owner_id="owner", action_id=action)
        assert page(conn, owner_id="owner")["items"] == []
        assert collect(conn)["created"] == 0
        assert db.migrate(conn) == []
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
