"""Explicit, file-backed scale gate; opt in to its substantial IO workload."""

import os
import time
import threading

import pytest

from k3_support.db import connect, migrate, transaction
from k3_support.store import ingest_event
from k3_support.retention_reference_maintenance import tick
from k3_support.body_retention import preview, clear_unreferenced_page
from k3_support.retention_reference_index import begin_build


@pytest.mark.skipif(os.environ.get('K3_RETENTION_SCALE_TEST') != '1', reason='explicit 100001-row IO gate')
@pytest.mark.parametrize('write_rate', [0, 10, 100])
def test_100001_file_rows_backfill_shadow_and_clear(tmp_path, record_property, write_rate):
    conn = connect(tmp_path / 'reference-scale.db')
    stop = threading.Event()
    writer = None
    latencies, writer_errors = [], []
    try:
        migrate(conn)
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('PRAGMA synchronous').fetchone()[0] == 2
        conn.execute('CREATE TABLE scale_reference(id INTEGER PRIMARY KEY,payload_json TEXT)')
        with transaction(conn):
            conn.executemany('INSERT INTO scale_reference VALUES(?,?)', ((n, '{}') for n in range(100001)))
        event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='scale-old',
                                payload={'content': 'synthetic'}, occurred_at='2025-01-01T00:00:00Z')
        conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
        begin_build(conn)
        def write_changes():
            other = connect(tmp_path / 'reference-scale.db')
            origin = time.monotonic()
            try:
                for sequence in range(write_rate*30):
                    if stop.wait(max(0, origin+sequence/write_rate-time.monotonic())):
                        break
                    start = time.monotonic()
                    other.execute('UPDATE scale_reference SET payload_json=? WHERE id=?',
                                  ('{}', sequence % 100001))
                    latencies.append(time.monotonic()-start)
            except Exception as exc:
                writer_errors.append(type(exc).__name__)
            finally:
                other.close()
        if write_rate:
            writer = threading.Thread(target=write_changes, daemon=True)
            writer.start()
        started = time.monotonic()
        totals = {'backfilled': 0, 'consumed': 0, 'shadow_scanned': 0}
        peak_dirty = 0
        backlog_samples = []
        last_progress = started
        for batch in range(2000):
            result = tick(conn, max_batches=100, max_seconds=10)
            for key in totals:
                totals[key] += result[key]
            if any(result[key] for key in totals):
                last_progress = time.monotonic()
            assert not result['errors'], result
            dirty = conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0]
            peak_dirty = max(peak_dirty, dirty)
            backlog_samples.append((round(time.monotonic()-started, 3), dirty))
            assert time.monotonic()-last_progress < 30, result
            assert time.monotonic()-started < 600, result
            if result['ready']:
                break
        assert result['ready'], result
        record_property('ready_while_writer_running', bool(writer and writer.is_alive()))
        stop.set()
        if writer:
            writer.join(5)
            assert not writer.is_alive()
            assert not writer_errors
            assert len(latencies) >= write_rate*5
            record_property('writer_samples', len(latencies))
            p95 = sorted(latencies)[int((len(latencies)-1)*0.95)]
            record_property('writer_p95_seconds', p95)
            assert p95 < 0.05
            # A final write may have committed after the observed ready snapshot.
            for _ in range(20):
                if tick(conn, max_batches=100, max_seconds=10)['ready']:
                    break
        record_property('write_rate', write_rate)
        record_property('peak_dirty_at_tick_boundary', peak_dirty)
        record_property('last_ten_backlog_samples', backlog_samples[-10:])
        for key in totals:
            assert totals[key] >= 100001
            record_property(key, totals[key])
        record_property('maintenance_seconds', time.monotonic()-started)
        record_property('maintenance_ticks', batch+1)
        assert conn.execute('SELECT count(*) FROM retention_reference_dirty').fetchone()[0] == 0
        item = next(row for row in preview(conn, days=30)['items'] if row['event_pk'] == event)
        assert not item['clear_blockers']
        clear_started = time.monotonic()
        assert clear_unreferenced_page(conn, days=30, expected={event: item['snapshot_digest']}, actor='scale-test')['cleared'] == 1
        record_property('clear_seconds', time.monotonic()-clear_started)
        assert conn.execute('SELECT payload_json FROM inbound_events WHERE event_pk=?', (event,)).fetchone()[0] == '{}'
    finally:
        stop.set()
        if writer:
            writer.join(5)
        conn.close()
