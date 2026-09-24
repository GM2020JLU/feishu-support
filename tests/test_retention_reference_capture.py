import pytest

from k3_support.db import transaction
from k3_support.retention_reference_capture import install_source
from k3_support.retention_reference_sources import inventory


def setup_source(conn):
    conn.execute('CREATE TABLE synthetic_refs(id TEXT PRIMARY KEY, payload_json TEXT)')
    source = next(s for s in inventory(conn)['sources'] if s.table == 'synthetic_refs')
    with transaction(conn):
        ident = install_source(conn, source)
        assert install_source(conn, source) == ident
    return ident


def test_capture_preserves_revisions_across_delete_reinsert_and_key_change(conn):
    ident = setup_source(conn)
    conn.execute("INSERT INTO synthetic_refs VALUES('one','invalid JSON is captured, not parsed')")
    def row(key):
        return tuple(conn.execute('''SELECT revision,deleted,state FROM retention_reference_rows
            WHERE source_id=? AND row_key=?''', (ident, '["'+key+'"]')).fetchone())
    assert row('one') == (1, 0, 'pending')
    conn.execute("UPDATE synthetic_refs SET payload_json='{}' WHERE id='one'")
    assert row('one') == (3, 0, 'pending')
    conn.execute("DELETE FROM synthetic_refs WHERE id='one'")
    assert row('one') == (4, 1, 'pending')
    conn.execute("INSERT INTO synthetic_refs VALUES('one','{}')")
    assert row('one') == (5, 0, 'pending')
    conn.execute("UPDATE synthetic_refs SET id='two' WHERE id='one'")
    assert row('one') == (6, 1, 'pending')
    assert row('two') == (1, 0, 'pending')
    assert conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM retention_reference_generations').fetchone()[0] == 0


def test_source_write_rollback_also_rolls_back_dirty_and_revision(conn):
    setup_source(conn)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO synthetic_refs VALUES('one','{}')")
            raise RuntimeError('crash before commit')
    assert conn.execute('SELECT count(*) FROM retention_reference_rows').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM synthetic_refs').fetchone()[0] == 0


def test_capture_install_requires_transaction_and_rejects_modified_trigger(conn):
    setup_source(conn)
    source = next(s for s in inventory(conn)['sources'] if s.table == 'synthetic_refs')
    with pytest.raises(ValueError, match='transaction'):
        install_source(conn, source)
    trigger = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'retention_capture_%' LIMIT 1").fetchone()[0]
    conn.execute(f'DROP TRIGGER "{trigger}"')
    conn.execute(f'CREATE TRIGGER "{trigger}" AFTER INSERT ON synthetic_refs BEGIN SELECT 1; END')
    with pytest.raises(ValueError, match='trigger changed'):
        with transaction(conn):
            install_source(conn, source)
