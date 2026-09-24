from test_retention_reference_capture import setup_source

from k3_support.retention_reference_extract import reference_digest
from k3_support.retention_reference_worker import consume_dirty


def edges(conn):
    return {row[0] for row in conn.execute('SELECT target_digest FROM retention_reference_edges')}


def test_dirty_updates_replace_edges_and_deleted_rows_keep_tombstone(conn):
    setup_source(conn)
    conn.execute("INSERT INTO synthetic_refs VALUES('one','[\"future\"]')")
    assert consume_dirty(conn) == {'processed': 1, 'errors': 0, 'stale': 0}
    assert edges(conn) == {reference_digest('future')}
    conn.execute("UPDATE synthetic_refs SET payload_json='[\"new\"]'")
    consume_dirty(conn)
    assert edges(conn) == {reference_digest('new')}
    conn.execute('DELETE FROM synthetic_refs')
    consume_dirty(conn)
    assert not edges(conn)
    assert tuple(conn.execute('SELECT revision,deleted,state FROM retention_reference_rows').fetchone()) == (4, 1, 'complete')


def test_invalid_json_blocks_row_completion_and_keeps_old_edges(conn):
    setup_source(conn)
    conn.execute("INSERT INTO synthetic_refs VALUES('one','[\"protected\"]')")
    consume_dirty(conn)
    conn.execute("UPDATE synthetic_refs SET payload_json='invalid'")
    assert consume_dirty(conn)['errors'] == 1
    assert edges(conn) == {reference_digest('protected')}
    assert conn.execute('SELECT state FROM retention_reference_rows').fetchone()[0] == 'error'


def test_late_extraction_cannot_overwrite_recreated_source(conn, monkeypatch):
    from k3_support import retention_reference_worker as worker
    setup_source(conn)
    conn.execute("INSERT INTO synthetic_refs VALUES('one','[\"old\"]')")
    original = worker.extract_json
    def changed(value):
        assert not conn.in_transaction
        conn.execute('DELETE FROM synthetic_refs')
        conn.execute("INSERT INTO synthetic_refs VALUES('one','[\"new\"]')")
        return original(value)
    monkeypatch.setattr(worker, 'extract_json', changed)
    assert consume_dirty(conn) == {'processed': 0, 'errors': 0, 'stale': 1}
    assert not edges(conn)
    assert conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0] == 1
    monkeypatch.setattr(worker, 'extract_json', original)
    consume_dirty(conn)
    assert edges(conn) == {reference_digest('new')}


def test_oversized_value_is_not_passed_to_extractor(conn, monkeypatch):
    from k3_support import retention_reference_worker as worker
    setup_source(conn)
    conn.execute('INSERT INTO synthetic_refs VALUES(?,?)', ('one', '"'+'x'*100+'"'))
    monkeypatch.setattr(worker, 'MAX_VALUE_BYTES', 32)
    def forbidden(value):
        raise AssertionError('oversized source reached extractor')
    monkeypatch.setattr(worker, 'extract_json', forbidden)
    assert consume_dirty(conn)['errors'] == 1
    assert conn.execute('SELECT error_class FROM retention_reference_rows').fetchone()[0] == 'source_value_budget_exceeded'


def test_aggregate_snapshot_budget_leaves_unread_rows_dirty_and_progresses(conn, monkeypatch):
    from k3_support import retention_reference_worker as worker
    setup_source(conn)
    monkeypatch.setattr(worker, 'MAX_VALUE_BYTES', 16)
    monkeypatch.setattr(worker, 'MAX_BATCH_BYTES', 16)
    conn.executemany('INSERT INTO synthetic_refs VALUES(?,?)', [('one', '"12345678"'), ('two', '"abcdefgh"')])
    assert consume_dirty(conn)['processed'] == 1
    assert conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0] == 1
    assert consume_dirty(conn)['processed'] == 1
    assert conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0] == 0


def test_changed_rows_reverify_without_restarting_source_scan(conn):
    setup_source(conn)
    conn.execute("INSERT INTO synthetic_refs VALUES('one','[\"first\"]')")
    consume_dirty(conn)
    assert conn.execute('SELECT shadow_revision=revision FROM retention_reference_rows').fetchone()[0] == 1
    conn.execute("UPDATE synthetic_refs SET payload_json='[\"second\"]'")
    assert conn.execute('SELECT shadow_revision FROM retention_reference_rows').fetchone()[0] is None
    consume_dirty(conn)
    assert conn.execute('SELECT shadow_revision=revision FROM retention_reference_rows').fetchone()[0] == 1
    conn.execute('DELETE FROM synthetic_refs')
    consume_dirty(conn)
    assert conn.execute('SELECT shadow_revision=revision FROM retention_reference_rows').fetchone()[0] == 1


def test_production_extractor_omission_is_blocked_by_independent_oracle(conn, monkeypatch):
    from k3_support import retention_reference_worker as worker
    setup_source(conn)
    conn.execute("INSERT INTO synthetic_refs VALUES('one','[\"protected\"]')")
    monkeypatch.setattr(worker, 'extract_json', lambda value: frozenset())
    assert consume_dirty(conn)['errors'] == 1
    assert tuple(conn.execute('SELECT state,error_class,shadow_revision FROM retention_reference_rows').fetchone()) == ('error', 'shadow_edge_mismatch', None)
