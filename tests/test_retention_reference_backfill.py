import pytest

from k3_support.db import transaction
from k3_support.retention_reference_backfill import backfill_source
from k3_support.retention_reference_capture import install_source
from k3_support.retention_reference_sources import inventory
from k3_support.retention_reference_worker import consume_dirty
from k3_support.retention_reference_extract import reference_digest


def source(conn):
    selected = next(s for s in inventory(conn)['sources'] if s.table == 'synthetic_backfill')
    with transaction(conn):
        return install_source(conn, selected)


def test_existing_rows_backfill_in_batches_and_later_insert_behind_cursor_is_captured(conn):
    conn.execute('CREATE TABLE synthetic_backfill(id TEXT PRIMARY KEY, payload_json TEXT)')
    conn.executemany('INSERT INTO synthetic_backfill VALUES(?,?)',
                     [('b','["b-ref"]'), ('c','["c-ref"]'), ('d','["d-ref"]')])
    ident = source(conn)
    assert backfill_source(conn, ident, limit=1) == {'scanned': 1, 'complete': False}
    assert conn.execute('SELECT backfill_cursor FROM retention_reference_sources').fetchone()[0] == '["b"]'
    conn.execute('INSERT INTO synthetic_backfill VALUES(?,?)', ('a', '["a-ref"]'))
    conn.execute('UPDATE synthetic_backfill SET payload_json=? WHERE id=?', ('["new-c"]', 'c'))
    revision = conn.execute('SELECT revision FROM retention_reference_rows WHERE row_key=?', ('["c"]',)).fetchone()[0]
    assert backfill_source(conn, ident, limit=1)['scanned'] == 1
    assert conn.execute('SELECT revision FROM retention_reference_rows WHERE row_key=?', ('["c"]',)).fetchone()[0] == revision
    assert backfill_source(conn, ident, limit=1) == {'scanned': 1, 'complete': True}
    assert backfill_source(conn, ident, limit=1) == {'scanned': 0, 'complete': True}
    consume_dirty(conn)
    assert {r[0] for r in conn.execute('SELECT target_digest FROM retention_reference_edges')} == {
        reference_digest(value) for value in ('a-ref','b-ref','new-c','d-ref')}


def test_composite_keyset_and_empty_source(conn):
    conn.execute('CREATE TABLE synthetic_backfill(a TEXT,b INTEGER,payload_json TEXT,PRIMARY KEY(a,b))')
    conn.executemany('INSERT INTO synthetic_backfill VALUES(?,?,?)', [('x',2,'{}'),('x',1,'{}'),('y',1,'{}')])
    ident = source(conn)
    assert not backfill_source(conn, ident, limit=2)['complete']
    assert conn.execute('SELECT backfill_cursor FROM retention_reference_sources').fetchone()[0] == '["x",2]'
    assert backfill_source(conn, ident, limit=2) == {'scanned': 1, 'complete': True}


def test_null_historical_key_rolls_back_checkpoint(conn):
    conn.execute('CREATE TABLE synthetic_backfill(id TEXT PRIMARY KEY,payload_json TEXT)')
    conn.execute("INSERT INTO synthetic_backfill VALUES(NULL,'{}')")
    ident = source(conn)
    with pytest.raises(ValueError, match='null source'):
        backfill_source(conn, ident)
    assert tuple(conn.execute('SELECT backfill_cursor,backfill_complete FROM retention_reference_sources').fetchone()) == (None, 0)
    assert conn.execute('SELECT count(*) FROM retention_reference_rows').fetchone()[0] == 0
