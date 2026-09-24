import pytest

from k3_support import db


def test_generation_migration_is_atomic_and_does_not_approve_existing_data(tmp_path, monkeypatch):
    files = db.migration_files()
    old = [item for item in files if item[0] < 99]
    monkeypatch.setattr(db, 'migration_files', lambda: old)
    conn = db.connect(tmp_path / 'corpus-upgrade.db')
    try:
        db.migrate(conn)
        broken = [(version, name, sql+'\nBAD SQL;' if version == 99 else sql) for version, name, sql in files]
        monkeypatch.setattr(db, 'migration_files', lambda: broken)
        with pytest.raises(db.DatabaseError, match='099'):
            db.migrate(conn)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_corpus_%' OR name LIKE 'corpus_change_%'").fetchall() == []
        monkeypatch.setattr(db, 'migration_files', lambda: files)
        assert 99 in db.migrate(conn)
        assert tuple(conn.execute('SELECT revision,built_revision,corpus_digest FROM knowledge_corpus_state').fetchone()) == (1, None, None)
        assert conn.execute('SELECT count(*) FROM knowledge_corpus_builds').fetchone()[0] == 0
        monkeypatch.setattr(db, 'migration_files', lambda: old)
        with pytest.raises(db.DatabaseError, match='newer than this application'):
            db.migrate(conn)
    finally:
        conn.close()
