import os
from datetime import UTC, datetime

from test_broker_task_binding import seed

from k3_support import db
from k3_support.broker_grants import issue, verify_bound_task


def test_broker_upgrade_does_not_auto_grant_existing_jobs(tmp_path, monkeypatch):
    files = [item for item in db.migration_files() if item[0] <= 62]
    monkeypatch.setattr(db, "migration_files", lambda: [item for item in files if item[0] <= 61])
    conn = db.connect(tmp_path / "old.db")
    try:
        db.migrate(conn)
        params = seed(conn)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')] for table in tables if table != "schema_migrations"}
        monkeypatch.setattr(db, "migration_files", lambda: files)
        assert db.migrate(conn) == [62]
        for table, rows in before.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')] == rows
        assert conn.execute("SELECT count(*) FROM broker_grants").fetchone()[0] == 0
        now = datetime(2026, 9, 8, tzinfo=UTC)
        with db.transaction(conn):
            issued = issue(conn, job_id="job-1", attempt_no=1, lease_owner="worker", worker_uid=os.geteuid()+1, now=now)
            params["lease_token"] = issued["token"]
            assert verify_bound_task(conn, params, peer_uid=os.geteuid()+1, now=now)["grant_id"] == issued["grant_id"]
        assert db.migrate(conn) == []
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
