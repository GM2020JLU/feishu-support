import pytest
from test_routing import active_config

from k3_support import retention_worker
from k3_support.store import ingest_event


def ready_index(conn):
    from k3_support.retention_reference_maintenance import tick
    for _ in range(20):
        result = tick(conn, max_batches=100, max_seconds=10)
        if result['ready']:
            return
    pytest.fail(f'index did not become ready: {result}')


def seed(conn, identifier):
    event, _ = ingest_event(conn, source='feishu_user_poll', identity='user', external_id=identifier,
                           payload={'content': 'private old body'}, occurred_at='2025-01-01T00:00:00Z')
    conn.execute("UPDATE inbound_events SET received_epoch=0,status='processed' WHERE event_pk=?", (event,))
    return event


def test_worker_disabled_by_default_has_no_writes(conn, config):
    seed(conn, 'old')
    before = conn.serialize()
    assert retention_worker.BodyRetentionWorker().tick(conn, config)['skipped']
    assert conn.serialize() == before


def test_health_reports_disabled_without_clearing(conn, config):
    seed(conn, 'old')
    result = retention_worker.BodyRetentionWorker().tick_with_health(conn, config)
    assert result['skipped']
    row = conn.execute("SELECT status,detail_json FROM service_state WHERE component='body_retention'").fetchone()
    assert row['status'] == 'ready'
    assert 'disabled_or_not_active' in row['detail_json']
    assert 'private old body' not in row['detail_json']
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0


def test_scheduled_worker_builds_index_before_first_clear(conn, config):
    cfg = active_config(config)
    cfg.raw['policy']['body_retention_days'] = 30
    identifier = seed(conn, 'scheduled-index')
    worker = retention_worker.BodyRetentionWorker()
    first = worker.tick(conn, cfg)
    assert first['cleared'] == 0
    assert first['reason'] == 'dependency_scan_incomplete'
    assert first['index_progress']['backfilled'] >= 0
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0
    for _ in range(100):
        result = worker.tick(conn, cfg)
        if result['cleared']:
            break
    assert result['cleared'] == 1
    assert conn.execute('SELECT payload_json FROM inbound_events WHERE event_pk=?', (identifier,)).fetchone()[0] == '{}'


def test_worker_bounded_pages_and_receipts_are_idempotent(conn, config):
    cfg = active_config(config)
    cfg.raw['policy']['body_retention_days'] = 30
    for index in range(12):
        seed(conn, str(index))
    ready_index(conn)
    worker = retention_worker.BodyRetentionWorker()
    first = worker.tick(conn, cfg)
    assert first['cleared'] == 10 and not first['scan_cycle_complete']
    assert worker.tick(conn, cfg)['cleared'] == 2
    assert worker.tick(conn, cfg)['cleared'] == 0
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 12
    assert conn.execute("SELECT count(*) FROM inbound_events WHERE payload_json='{}'").fetchone()[0] == 12


def test_worker_rechecks_pause_before_clearing(conn, config, monkeypatch):
    cfg = active_config(config)
    cfg.raw['policy']['body_retention_days'] = 30
    identifier = seed(conn, 'old')
    ready_index(conn)
    checks = iter([True, False])
    monkeypatch.setattr(retention_worker, 'capability_allowed', lambda *args: next(checks))
    result = retention_worker.BodyRetentionWorker().tick(conn, cfg)
    assert result['cleared'] == 0
    assert 'private old body' in conn.execute('SELECT payload_json FROM inbound_events WHERE event_pk=?', (identifier,)).fetchone()[0]
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0


def test_worker_preserves_late_claim(conn, config, monkeypatch):
    cfg = active_config(config)
    cfg.raw['policy']['body_retention_days'] = 30
    identifier = seed(conn, 'old')
    ready_index(conn)
    original = retention_worker.preview

    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        conn.execute("UPDATE inbound_events SET status='claimed',lease_owner='other' WHERE event_pk=?", (identifier,))
        return result

    monkeypatch.setattr(retention_worker, 'preview', preview)
    assert retention_worker.BodyRetentionWorker().tick(conn, cfg)['cleared'] == 0
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0


@pytest.mark.parametrize('days', [True, 0, 3651, '30'])
def test_invalid_worker_policy_rejected(conn, config, days):
    cfg = active_config(config)
    cfg.raw['policy']['body_retention_days'] = days
    with pytest.raises(ValueError):
        retention_worker.BodyRetentionWorker().tick(conn, cfg)


def test_changed_first_page_does_not_starve_later_bodies(conn, config, monkeypatch):
    cfg = active_config(config)
    cfg.raw['policy']['body_retention_days'] = 30
    for index in range(12):
        seed(conn, str(index))
    ready_index(conn)
    original = retention_worker.preview
    first_page = []

    def racing_preview(*args, **kwargs):
        page = original(*args, **kwargs)
        if not kwargs.get('after_id'):
            first_page[:] = [row['event_pk'] for row in page['items']]
            conn.execute("UPDATE inbound_events SET payload_json=json_set(payload_json,'$.changed',1) WHERE event_pk=?",
                         (first_page[0],))
        return page

    monkeypatch.setattr(retention_worker, 'preview', racing_preview)
    worker = retention_worker.BodyRetentionWorker()
    first = worker.tick(conn, cfg)
    assert first['cleared'] == 0 and not first['scan_cycle_complete']
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0
    second = worker.tick(conn, cfg)
    assert second['cleared'] == 2 and second['scan_cycle_complete']
    placeholders = ','.join('?' for _ in first_page)
    assert conn.execute(f'SELECT count(*) FROM body_retention_receipts WHERE event_pk IN ({placeholders})',
                        first_page).fetchone()[0] == 0
    monkeypatch.setattr(retention_worker, 'preview', original)
    assert worker.tick(conn, cfg)['cleared'] == 10
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 12
