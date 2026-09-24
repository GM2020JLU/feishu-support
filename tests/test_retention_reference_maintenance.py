from test_retention_reference_index import index_db

from k3_support.retention_reference_maintenance import tick


def test_bounded_ticks_preserve_progress_until_verified():
    conn = index_db()
    conn.executemany('INSERT INTO synthetic VALUES(?,?)', [(i, '{}') for i in range(2, 152)])
    first = tick(conn, max_batches=1, max_seconds=10)
    assert first['backfilled'] == 100
    assert not first['ready']
    previous = conn.execute('SELECT backfill_cursor FROM retention_reference_sources').fetchone()[0]
    second = tick(conn, max_batches=1, max_seconds=10)
    assert second['backfilled'] == 51
    assert conn.execute('SELECT backfill_cursor FROM retention_reference_sources').fetchone()[0] != previous
    for _ in range(20):
        result = tick(conn, max_batches=1, max_seconds=10)
        if result['ready']:
            break
    assert result['ready']
    assert tick(conn, max_batches=1)['consumed'] == 0


def test_invalid_row_is_reported_without_restarting_backfill():
    conn = index_db()
    conn.execute("UPDATE synthetic SET payload_json='invalid'")
    result = tick(conn, max_batches=1, max_seconds=10)
    assert not result['ready'] and result['errors']
    assert conn.execute('SELECT backfill_complete FROM retention_reference_sources').fetchone()[0] == 1
    again = tick(conn, max_batches=1, max_seconds=10)
    assert again['backfilled'] == 0
    assert not again['ready']


def test_write_between_diagnostic_and_promotion_defers_without_recovery_error(monkeypatch):
    from k3_support import retention_reference_maintenance as maintenance
    conn = index_db()
    original = maintenance.promote_verified
    def raced(connection):
        connection.execute('INSERT INTO synthetic VALUES(2,\'["new"]\')')
        return original(connection)
    monkeypatch.setattr(maintenance, 'promote_verified', raced)
    result = tick(conn, max_batches=5, max_seconds=10)
    assert not result['ready']
    assert any(b['reason'] == 'dirty_pending' for b in result['blockers'])
    monkeypatch.setattr(maintenance, 'promote_verified', original)
    assert tick(conn, max_batches=5, max_seconds=10)['ready']
