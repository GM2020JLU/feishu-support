from test_retention_reference_index import index_db

from k3_support.retention_reference_index import begin_build
from k3_support.retention_reference_backfill import backfill_source
from k3_support.retention_reference_worker import consume_dirty
from k3_support.retention_reference_shadow import scan_source
from k3_support.retention_reference_index import promote_verified, inspect_build
import pytest


def prepared():
    conn = index_db()
    conn.execute('INSERT INTO synthetic VALUES(2,\'["second"]\')')
    begin_build(conn)
    ident = conn.execute('SELECT source_id FROM retention_reference_sources').fetchone()[0]
    backfill_source(conn, ident)
    consume_dirty(conn)
    return conn, ident


def test_shadow_traverses_source_in_persistent_pages():
    conn, ident = prepared()
    assert scan_source(conn, ident, limit=1) == {'scanned': 1, 'complete': False}
    assert conn.execute('SELECT shadow_cursor FROM retention_reference_sources').fetchone()[0] == '[1]'
    assert scan_source(conn, ident, limit=1) == {'scanned': 1, 'complete': True}
    assert conn.execute('SELECT count(*) FROM retention_reference_rows WHERE shadow_revision=revision').fetchone()[0] == 2


def test_shadow_detects_missing_edges_and_does_not_advance():
    conn, ident = prepared()
    conn.execute('DELETE FROM retention_reference_edges WHERE row_key=\'[1]\'')
    assert scan_source(conn, ident)['error'] == 'shadow_edge_mismatch'
    assert conn.execute('SELECT shadow_cursor FROM retention_reference_sources').fetchone()[0] is None


def test_source_traversal_detects_entirely_missing_index_row():
    conn, ident = prepared()
    conn.execute('DELETE FROM retention_reference_edges WHERE row_key=\'[1]\'')
    conn.execute('DELETE FROM retention_reference_rows WHERE row_key=\'[1]\'')
    assert scan_source(conn, ident)['error'] == 'missing_or_incomplete_index_row'


def test_shadow_rejects_source_changed_during_oracle(conn, monkeypatch):
    from k3_support import retention_reference_shadow as shadow
    conn, ident = prepared()
    original = shadow.json_reference_digests
    def change(value):
        assert not conn.in_transaction
        conn.execute('UPDATE synthetic SET payload_json=\'["changed"]\' WHERE id=1')
        return original(value)
    monkeypatch.setattr(shadow, 'json_reference_digests', change)
    assert scan_source(conn, ident)['stale']
    assert conn.execute('SELECT shadow_cursor FROM retention_reference_sources').fetchone()[0] is None


def test_ready_requires_full_source_shadow_and_caught_up_changes():
    conn, ident = prepared()
    with pytest.raises(ValueError, match='incomplete'):
        promote_verified(conn)
    scan_source(conn, ident)
    assert promote_verified(conn)['ready']
    assert inspect_build(conn)['ready']
    conn.execute('INSERT INTO synthetic VALUES(0,\'["behind-cursor"]\')')
    assert not inspect_build(conn)['ready']
    with pytest.raises(ValueError, match='incomplete'):
        promote_verified(conn)
    consume_dirty(conn)
    assert inspect_build(conn)['ready']
    assert conn.execute('SELECT shadow_cursor FROM retention_reference_sources').fetchone()[0] == '[2]'


def test_pending_capture_defers_shadow_without_persisting_error():
    conn, ident = prepared()
    conn.execute('UPDATE synthetic SET payload_json=\'["updated"]\' WHERE id=1')
    assert scan_source(conn, ident)['pending']
    assert conn.execute('SELECT shadow_error FROM retention_reference_sources').fetchone()[0] is None
    consume_dirty(conn)
    assert scan_source(conn, ident)['complete']
