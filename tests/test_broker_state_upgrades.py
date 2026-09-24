import hashlib
from uuid import uuid4

import pytest
from test_broker_grant_schema import grant
from test_broker_task_binding import seed

from k3_support import db
from k3_support.ids import canonical_json


@pytest.mark.parametrize("version", [62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 94])
def test_populated_broker_versions_upgrade_without_rewriting_evidence(tmp_path, monkeypatch, version):
    files = db.migration_files()
    monkeypatch.setattr(db, "migration_files", lambda: [item for item in files if item[0] <= version])
    conn = db.connect(tmp_path / "upgrade.db")
    try:
        db.migrate(conn)
        params = seed(conn)
        grant(conn)
        if version >= 63:
            conn.execute("INSERT INTO broker_receipts VALUES(?,?,?,?,?,?,?)",
                         (1234, str(uuid4()), "a" * 64, "renew", "grant-1",
                          canonical_json({"accepted": True, "job_id": "job-1"}), "2026-09-08T00:30:00+00:00"))
        if version >= 64:
            report = "synthetic pre-upgrade report; not verified"
            conn.execute("INSERT INTO broker_results VALUES(?,?,?,?,?,?,?,?,?)",
                         ("grant-1", "job-1", 1, 1, params["input_digest"],
                          hashlib.sha256(report.encode()).hexdigest(), report,
                          canonical_json({"status": "partial"}), "2026-09-08T00:30:00+00:00"))
        if version >= 65:
            conn.execute("INSERT INTO broker_inputs VALUES(?,?,?)",
                         ("job-1", canonical_json({"brief": "synthetic historical snapshot"}), "2026-09-08T00:30:00+00:00"))
        if version >= 66:
            binding = {key: value for key, value in params.items() if key != "lease_token"}
            conn.execute("INSERT INTO broker_claim_receipts VALUES(?,?,?,?,?,?)",
                         (1234, str(uuid4()), "a" * 64, "b" * 64, canonical_json(binding), "2026-09-08T00:30:00+00:00"))
        if version >= 67:
            conn.execute("INSERT INTO broker_execution_starts VALUES(?,?,?,?,?,?)",
                         ("grant-1", "job-1", 1, str(uuid4()), 1234, "2026-09-08T00:30:00+00:00"))
        if version >= 79:
            board_request = str(uuid4())
            conn.execute("INSERT INTO broker_board_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (board_request, 1234, "grant-1", "historical-session", "a"*64, '{"type":"reset"}',
                          "b"*64, "c"*64, "queued", "historical", "historical"))
        if version >= 80:
            conn.execute("UPDATE broker_board_actions SET state='unknown' WHERE request_id=?", (board_request,))
            conn.execute("INSERT INTO broker_board_results VALUES(?,?,?,?,?)",
                         (board_request, 124, "partial serial observation", "timed out", "historical"))
        if version >= 81:
            conn.execute("INSERT INTO broker_board_cleanup VALUES(?,?,?,?,?)",
                         ("grant-1", "historical-session", "unknown", "historical", "historical"))
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before = {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')]
                  for name in tables if name != "schema_migrations"}
        monkeypatch.setattr(db, "migration_files", lambda: files)
        assert db.migrate(conn) == list(range(version+1, max(item[0] for item in files)+1))
        for name, rows in before.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')] == rows
        # Migration cannot manufacture permission to start historical work.
        assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == int(version >= 67)
        assert conn.execute("SELECT count(*) FROM broker_recovery_actions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM broker_execution_instances").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM broker_service_exits").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM broker_board_cleanup").fetchone()[0] == int(version >= 81)
        assert conn.execute("SELECT count(*) FROM retention_recovery_requests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM broker_execution_resources").fetchone()[0] == 0
        assert db.migrate(conn) == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
    finally:
        conn.close()


def test_resource_migration_interruption_is_atomic_and_old_binary_rejects_new_schema(tmp_path, monkeypatch):
    files = db.migration_files()
    old = [item for item in files if item[0] <= 94]
    migration = next(item for item in files if item[0] == 95)
    monkeypatch.setattr(db, 'migration_files', lambda: old)
    conn = db.connect(tmp_path / 'interrupted-resource-upgrade.db')
    try:
        db.migrate(conn)
        seed(conn)
        grant(conn)
        before = list(conn.iterdump())
        broken = (*migration[:2], migration[2] + '\nSELECT * FROM injected_missing_migration_table;')
        monkeypatch.setattr(db, 'migration_files', lambda: old + [broken])
        with pytest.raises(db.DatabaseError, match='migration .* failed'):
            db.migrate(conn)
        assert list(conn.iterdump()) == before
        monkeypatch.setattr(db, 'migration_files', lambda: files)
        assert 95 in db.migrate(conn)
        assert conn.execute('SELECT count(*) FROM broker_execution_resources').fetchone()[0] == 0
        monkeypatch.setattr(db, 'migration_files', lambda: old)
        before = list(conn.iterdump())
        with pytest.raises(db.DatabaseError, match='newer than this application'):
            db.migrate(conn)
        assert list(conn.iterdump()) == before
    finally:
        conn.close()


def test_launch_binding_upgrade_preserves_unknown_launch_and_is_atomic(tmp_path, monkeypatch):
    files = db.migration_files()
    old = [item for item in files if item[0] <= 102]
    migration = next(item for item in files if item[0] == 103)
    monkeypatch.setattr(db, 'migration_files', lambda: old)
    conn = db.connect(tmp_path / 'launch-upgrade.db')
    try:
        db.migrate(conn)
        launch_id = str(uuid4())
        conn.execute("INSERT INTO broker_launches VALUES(?,'unknown','synthetic','synthetic')", (launch_id,))
        before = list(conn.iterdump())
        broken = (*migration[:2], migration[2] + '\nSELECT * FROM injected_missing_migration_table;')
        monkeypatch.setattr(db, 'migration_files', lambda: old + [broken])
        with pytest.raises(db.DatabaseError, match='migration .* failed'):
            db.migrate(conn)
        assert list(conn.iterdump()) == before
        monkeypatch.setattr(db, 'migration_files', lambda: files)
        assert 103 in db.migrate(conn)
        assert conn.execute('SELECT count(*) FROM broker_launch_bindings').fetchone()[0] == 0
        assert tuple(conn.execute('SELECT * FROM broker_launches').fetchone()) == (
            launch_id, 'unknown', 'synthetic', 'synthetic')
        monkeypatch.setattr(db, 'migration_files', lambda: old)
        with pytest.raises(db.DatabaseError, match='newer than this application'):
            db.migrate(conn)
        assert db.integrity(conn)['ok']
    finally:
        conn.close()
