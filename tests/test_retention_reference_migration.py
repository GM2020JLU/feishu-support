import pytest

from k3_support import db


def test_reference_migration_interruption_rolls_back_and_old_binary_rejects(tmp_path, monkeypatch):
    files = db.migration_files()
    old = [item for item in files if item[0] <= 97]
    monkeypatch.setattr(db, 'migration_files', lambda: old)
    conn = db.connect(tmp_path / 'reference-upgrade.db')
    try:
        db.migrate(conn)
        # Unrelated user data must survive failed and successful upgrades.
        conn.execute('CREATE TABLE synthetic_upgrade_payload(id INTEGER PRIMARY KEY,body TEXT)')
        conn.execute("INSERT INTO synthetic_upgrade_payload VALUES(1,'preserve me')")
        broken = [(version, name, sql + '\nINVALID MIGRATION STATEMENT;' if version == 98 else sql)
                  for version, name, sql in files]
        monkeypatch.setattr(db, 'migration_files', lambda: broken)
        with pytest.raises(db.DatabaseError, match='098'):
            db.migrate(conn)
        assert not conn.in_transaction
        assert conn.execute('SELECT 1 FROM schema_migrations WHERE version=98').fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'retention_reference_%'").fetchall() == []
        monkeypatch.setattr(db, 'migration_files', lambda: files)
        assert db.migrate(conn) == [version for version, _, _ in files if version > 97]
        assert conn.execute('SELECT count(*) FROM retention_reference_generations').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM retention_reference_sources').fetchone()[0] == 0
        assert conn.execute('SELECT body FROM synthetic_upgrade_payload').fetchone()[0] == 'preserve me'
        monkeypatch.setattr(db, 'migration_files', lambda: old)
        with pytest.raises(db.DatabaseError, match='newer than this application'):
            db.migrate(conn)
    finally:
        conn.close()
