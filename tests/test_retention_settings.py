import pytest
from test_routing import active_config
from test_retention_worker import seed

from k3_support import retention_settings as settings
from k3_support.retention_worker import BodyRetentionWorker


def test_upgrade_86_to_current_preserves_bodies_and_keeps_policy_off(tmp_path, config, monkeypatch):
    from k3_support import db
    migrations = db.migration_files()
    monkeypatch.setattr(db, 'migration_files', lambda: [row for row in migrations if row[0] <= 86])
    conn = db.connect(tmp_path / 'upgrade.db')
    try:
        db.migrate(conn)
        seed(conn, 'old-body')
        before = [tuple(row) for row in conn.execute('SELECT * FROM inbound_events')]
        monkeypatch.setattr(db, 'migration_files', lambda: migrations)
        assert db.migrate(conn) == [row[0] for row in migrations if row[0] > 86]
        assert [tuple(row) for row in conn.execute('SELECT * FROM inbound_events')] == before
        assert settings.snapshot(conn, config)['days'] is None
        assert conn.execute('SELECT count(*) FROM body_retention_settings').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0
        assert db.integrity(conn)['ok']
    finally:
        conn.close()


def test_explicit_policy_apply_is_bound_and_worker_reads_fresh_values(conn, config):
    cfg = active_config(config)
    worker = BodyRetentionWorker()
    seed(conn, 'one')
    assert worker.tick(conn, cfg)['skipped']
    draft = settings.preview(conn, cfg, days=30, expected_revision=0, session_id='owner-session')
    with pytest.raises(ValueError):
        settings.apply(conn, cfg, draft_id=draft['draft_id'], session_id='other-session', actor_id='owner')
    result = settings.apply(conn, cfg, draft_id=draft['draft_id'], session_id='owner-session', actor_id='owner')
    assert result['bodies_cleared_by_this_request'] == 0
    assert settings.apply(conn, cfg, draft_id=draft['draft_id'], session_id='owner-session', actor_id='owner')['replayed']
    initial = worker.tick(conn, cfg)
    assert initial['cleared'] == 0 and initial['reason'] == 'dependency_scan_incomplete'
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 0
    for _ in range(100):
        result = worker.tick(conn, cfg)
        if result['cleared']:
            break
    assert result['cleared'] == 1
    seed(conn, 'two')
    off = settings.preview(conn, cfg, days=None, expected_revision=1, session_id='owner-session')
    settings.apply(conn, cfg, draft_id=off['draft_id'], session_id='owner-session', actor_id='owner')
    before = conn.serialize()
    assert worker.tick(conn, cfg)['skipped']
    assert conn.serialize() == before
    assert conn.execute('SELECT count(*) FROM body_retention_receipts').fetchone()[0] == 1


def test_draft_policy_is_separate_and_worker_reads_updates(conn, config):
    from test_draft_retention import seed as seed_draft
    from k3_support.draft_retention_worker import DraftRetentionWorker
    cfg = active_config(config)
    identifier = seed_draft(conn)
    worker = DraftRetentionWorker()
    assert worker.tick(conn, cfg)['cleared'] == 0
    draft = settings.preview(conn, cfg, days=30, expected_revision=0, session_id='s', scope='draft')
    with pytest.raises(ValueError):
        settings.apply(conn, cfg, draft_id=draft['draft_id'], session_id='s', actor_id='owner')
    with pytest.raises(ValueError):
        settings.apply(conn, cfg, draft_id=draft['draft_id'], session_id='other', actor_id='owner', scope='draft')
    settings.apply(conn, cfg, draft_id=draft['draft_id'], session_id='s', actor_id='owner', scope='draft')
    assert conn.execute('SELECT 1 FROM knowledge_authoring_drafts WHERE candidate_id=?', (identifier,)).fetchone()
    assert settings.snapshot(conn, cfg)['days'] is None
    assert worker.tick(conn, cfg)['cleared'] == 1
    off = settings.preview(conn, cfg, days=None, expected_revision=1, session_id='s', scope='draft')
    settings.apply(conn, cfg, draft_id=off['draft_id'], session_id='s', actor_id='owner', scope='draft')
    assert worker.tick(conn, cfg)['reason'] == 'disabled_or_not_active'


@pytest.mark.parametrize('scope,key', [('body', 'body_retention_days'), ('draft', 'captured_draft_retention_days')])
def test_stale_policy_draft_cannot_overwrite_newer_or_changed_base(conn, config, scope, key):
    first = settings.preview(conn, config, days=30, expected_revision=0, session_id='s', scope=scope)
    second = settings.preview(conn, config, days=60, expected_revision=0, session_id='s', scope=scope)
    settings.apply(conn, config, draft_id=first['draft_id'], session_id='s', actor_id='owner', scope=scope)
    with pytest.raises(ValueError, match='失效'):
        settings.apply(conn, config, draft_id=second['draft_id'], session_id='s', actor_id='owner', scope=scope)
    config.raw['policy'][key] = 90
    assert settings.snapshot(conn, config, scope=scope)['needs_migration']
    assert settings.snapshot(conn, config, scope=scope)['days'] is None
    with pytest.raises(ValueError, match='先明确关闭'):
        settings.preview(conn, config, days=30, expected_revision=1, session_id='s', scope=scope)
    off = settings.preview(conn, config, days=None, expected_revision=1, session_id='s', scope=scope)
    settings.apply(conn, config, draft_id=off['draft_id'], session_id='s', actor_id='owner', scope=scope)
    assert not settings.snapshot(conn, config, scope=scope)['needs_migration']


@pytest.mark.parametrize('scope', ['body', 'draft'])
def test_expired_policy_draft_is_not_applied(conn, config, scope):
    draft = settings.preview(conn, config, days=30, expected_revision=0, session_id='s', scope=scope)
    conn.execute(f"UPDATE {scope}_retention_policy_drafts SET expires_at='2000-01-01T00:00:00Z'")
    with pytest.raises(ValueError, match='失效'):
        settings.apply(conn, config, draft_id=draft['draft_id'], session_id='s', actor_id='owner', scope=scope)
    assert settings.snapshot(conn, config, scope=scope)['revision'] == 0


def test_upgrade_preserves_existing_body_override_and_draft_content(tmp_path, config, monkeypatch):
    from k3_support import db
    from test_draft_retention import seed as seed_draft
    migrations = db.migration_files()
    monkeypatch.setattr(db, 'migration_files', lambda: [row for row in migrations if row[0] <= 87])
    conn = db.connect(tmp_path / 'upgrade87.db')
    try:
        db.migrate(conn)
        seed_draft(conn)
        draft = settings.preview(conn, config, days=30, expected_revision=0, session_id='s')
        settings.apply(conn, config, draft_id=draft['draft_id'], session_id='s', actor_id='owner')
        before = [tuple(row) for row in conn.execute('SELECT * FROM knowledge_authoring_drafts')]
        body_before = settings.snapshot(conn, config)
        monkeypatch.setattr(db, 'migration_files', lambda: migrations)
        assert db.migrate(conn) == [row[0] for row in migrations if row[0] > 87]
        assert settings.snapshot(conn, config) == body_before
        assert settings.snapshot(conn, config, scope='draft')['days'] is None
        assert [tuple(row) for row in conn.execute('SELECT * FROM knowledge_authoring_drafts')] == before
        assert db.integrity(conn)['ok']
    finally:
        conn.close()
