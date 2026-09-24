import pytest

from k3_support.body_retention import preview, clear_unreferenced_page
from k3_support.store import ingest_event
from k3_support.retention_reference_index import begin_build, promote_verified
from k3_support.retention_reference_backfill import backfill_source
from k3_support.retention_reference_worker import consume_dirty
from k3_support.retention_reference_shadow import scan_source


def build(conn):
    begin_build(conn)
    sources = [row[0] for row in conn.execute('SELECT source_id FROM retention_reference_sources')]
    for source in sources:
        while not backfill_source(conn, source)['complete']:
            pass
    while consume_dirty(conn)['processed']:
        pass
    for source in sources:
        while not scan_source(conn, source)['complete']:
            pass
    promote_verified(conn)


def test_indexed_clear_keeps_all_guards_and_never_runs_legacy_scan(conn, monkeypatch):
    from k3_support import body_retention
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='indexed-old',
                            payload={'message_id': 'indexed-old', 'content': 'private'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    build(conn)
    def forbidden(*args, **kwargs):
        raise AssertionError('indexed mode must not scan all JSON')
    monkeypatch.setattr(body_retention, '_json_dependencies', forbidden)
    item = preview(conn, days=30)['items'][0]
    assert not item['clear_blockers']
    result = clear_unreferenced_page(conn, days=30, expected={event: item['snapshot_digest']}, actor='test')
    assert result['cleared'] == 1
    assert conn.execute('SELECT payload_json FROM inbound_events WHERE event_pk=?', (event,)).fetchone()[0] == '{}'


def test_new_reference_after_preview_blocks_clear_even_before_index_catches_up(conn):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id='indexed-reference',
                            payload={'content': 'private'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    conn.execute('CREATE TABLE synthetic_reference(id INTEGER PRIMARY KEY, payload_json TEXT)')
    build(conn)
    item = preview(conn, days=30)['items'][0]
    conn.execute('INSERT INTO synthetic_reference VALUES(1,?)', ('["'+event+'"]',))
    with pytest.raises(ValueError, match='incomplete'):
        clear_unreferenced_page(conn, days=30, expected={event: item['snapshot_digest']}, actor='test')
    consume_dirty(conn)
    current = preview(conn, days=30)['items'][0]
    assert 'referenced' in current['clear_blockers']
    assert current['snapshot_digest'] != item['snapshot_digest']
