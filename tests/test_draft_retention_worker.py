import copy

import pytest
from test_draft_retention import seed
from test_routing import active_config

from k3_support import draft_retention_worker as worker
from k3_support.config import ConfigError, validate_config


def configured(config):
    cfg = active_config(config)
    cfg.raw['policy']['captured_draft_retention_days'] = 30
    return cfg


def test_default_off_never_clears(conn, config):
    seed(conn)
    before = conn.serialize()
    assert worker.DraftRetentionWorker().tick(conn, active_config(config))['cleared'] == 0
    assert conn.serialize() == before


def test_enabled_draft_cleanup_is_audited_and_idempotent(conn, config):
    identifier = seed(conn)
    instance = worker.DraftRetentionWorker()
    cfg = configured(config)
    assert instance.tick(conn, cfg)['cleared'] == 1
    assert instance.tick(conn, cfg)['cleared'] == 0
    assert conn.execute('SELECT artifact_path FROM retention_tombstones').fetchone()[0] == identifier


@pytest.mark.parametrize('change', ['policy', 'gui_policy', 'reference', 'mode'])
def test_change_after_preview_prevents_clear(conn, config, monkeypatch, change):
    identifier = seed(conn)
    cfg = configured(config)
    original = worker.clear
    def race(db, **kwargs):
        if change == 'policy':
            cfg.raw['policy']['captured_draft_retention_days'] = None
        elif change == 'gui_policy':
            from k3_support import retention_settings
            draft = retention_settings.preview(db, cfg, days=None, expected_revision=0,
                                               session_id='owner', scope='draft')
            retention_settings.apply(db, cfg, draft_id=draft['draft_id'], session_id='owner',
                                     actor_id='owner', scope='draft')
        elif change == 'mode':
            cfg.raw['mode'] = 'shadow'
        else:
            db.execute('CREATE TABLE fixture_reference(payload_json TEXT)')
            db.execute('INSERT INTO fixture_reference VALUES(json_object(?,?))', ('draft', identifier))
        return original(db, **kwargs)
    monkeypatch.setattr(worker, 'clear', race)
    assert worker.DraftRetentionWorker().tick(conn, cfg)['cleared'] == 0
    assert conn.execute('SELECT 1 FROM knowledge_authoring_drafts').fetchone()
    assert not conn.execute('SELECT 1 FROM retention_tombstones').fetchone()


@pytest.mark.parametrize('days', [True, 0, -1, 3651, '30', 1.5])
def test_invalid_draft_policy(config, days):
    raw = copy.deepcopy(config.raw)
    raw['policy']['captured_draft_retention_days'] = days
    with pytest.raises(ConfigError):
        validate_config(raw)


def test_retained_first_draft_does_not_starve_later_draft(conn, config):
    from test_knowledge_authoring import fields
    from k3_support.docling_draft import build_draft
    from k3_support.knowledge_authoring import save_attachment
    seed(conn)
    value = fields()
    value['title'] = 'Independent unreviewed draft'
    value['answer'] = 'Another unreviewed draft'
    draft = build_draft(**value)
    save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    rows = conn.execute('SELECT candidate_id FROM knowledge_authoring_drafts ORDER BY candidate_id').fetchall()
    conn.execute("UPDATE knowledge_authoring_drafts SET saved_at='2020-01-01T00:00:00+00:00'")
    conn.execute("UPDATE knowledge_authoring_drafts SET saved_at='2099-01-01T00:00:00+00:00' WHERE candidate_id=?", (rows[0][0],))
    instance, cfg = worker.DraftRetentionWorker(), configured(config)
    assert instance.tick(conn, cfg)['reason'] == 'retained'
    assert instance.tick(conn, cfg)['cleared'] == 1
    assert conn.execute('SELECT candidate_id FROM knowledge_authoring_drafts').fetchone()[0] == rows[0][0]


def test_watchdog_requires_draft_worker_only_when_enabled(conn, config):
    from k3_support.watchdog import collect_health_alerts
    cfg = active_config(config)
    assert not any(a['key'] == 'heartbeat_missing:draft_retention' for a in collect_health_alerts(conn, cfg))
    cfg.raw['policy']['captured_draft_retention_days'] = 30
    assert any(a['key'] == 'heartbeat_missing:draft_retention' for a in collect_health_alerts(conn, cfg))
    worker.DraftRetentionWorker().tick_with_health(conn, cfg)
    assert not any(a['key'] == 'heartbeat_missing:draft_retention' for a in collect_health_alerts(conn, cfg))


@pytest.mark.parametrize('mode', ['paused', 'stopped'])
def test_real_control_callback_after_preview_blocks_clear(conn, config, monkeypatch, mode):
    from k3_support.replay_snapshot import replay_snapshot
    from k3_support.replay_modes import switch_mode
    seed(conn)
    cfg = configured(config)
    cfg.raw['identity']['telegram_control_user_id'] = 'owner'
    cfg.raw['identity']['telegram_control_chat_id'] = 'chat'
    original = worker.clear
    def pause(db, **kwargs):
        switch_mode(db, cfg, mode=mode, step_id='retention-race')
        return original(db, **kwargs)
    monkeypatch.setattr(worker, 'clear', pause)
    with replay_snapshot(cfg.database_path) as snapshot:
        assert worker.DraftRetentionWorker().tick(snapshot, cfg)['cleared'] == 0
        assert snapshot.execute('SELECT 1 FROM knowledge_authoring_drafts').fetchone()
        assert not snapshot.execute('SELECT 1 FROM retention_tombstones').fetchone()


@pytest.mark.parametrize('metadata', ['[]', 'null', '"text"', '{}', '{"id":[]}', '{"id":""}'])
def test_malformed_identity_is_retained_and_health_is_degraded(conn, config, metadata):
    identifier = seed(conn)
    conn.execute('UPDATE knowledge_authoring_drafts SET metadata_json=? WHERE candidate_id=?', (metadata, identifier))
    result = worker.DraftRetentionWorker().tick_with_health(conn, configured(config))
    assert result['cleared'] == 0 and result['needs_attention']
    assert conn.execute("SELECT status FROM service_state WHERE component='draft_retention'").fetchone()[0] == 'degraded'
    assert conn.execute('SELECT 1 FROM knowledge_authoring_drafts').fetchone()
    assert not conn.execute('SELECT 1 FROM retention_tombstones').fetchone()


def test_invalid_first_candidate_does_not_crash_or_block_later_candidate(conn, config):
    from test_knowledge_authoring import fields
    from k3_support.docling_draft import build_draft
    from k3_support.knowledge_authoring import save_attachment
    seed(conn)
    value = fields()
    value['title'] = 'Independent capture'
    draft = build_draft(**value)
    save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    ids = [r[0] for r in conn.execute('SELECT candidate_id FROM knowledge_authoring_drafts ORDER BY candidate_id')]
    conn.execute("UPDATE knowledge_authoring_drafts SET saved_at='2020-01-01T00:00:00+00:00'")
    conn.execute("UPDATE knowledge_authoring_drafts SET metadata_json='[]' WHERE candidate_id=?", (ids[0],))
    instance, cfg = worker.DraftRetentionWorker(), configured(config)
    assert instance.tick(conn, cfg)['needs_attention']
    assert instance.tick(conn, cfg)['cleared'] == 1
    assert conn.execute('SELECT candidate_id FROM knowledge_authoring_drafts').fetchone()[0] == ids[0]


def test_health_recovers_only_after_complete_clean_cycle(conn, config):
    from test_knowledge_authoring import fields
    from k3_support.docling_draft import build_draft
    from k3_support.knowledge_authoring import save_attachment
    seed(conn)
    value = fields()
    value['title'] = 'Separate healthy row'
    draft = build_draft(**value)
    save_attachment(conn, fields=value, expected_digest=draft['revision_digest'], actor_id='owner')
    rows = conn.execute('SELECT candidate_id,metadata_json FROM knowledge_authoring_drafts ORDER BY candidate_id').fetchall()
    conn.execute("UPDATE knowledge_authoring_drafts SET saved_at='2099-01-01T00:00:00+00:00'")
    conn.execute("UPDATE knowledge_authoring_drafts SET metadata_json='[]' WHERE candidate_id=?", (rows[0][0],))
    instance, cfg = worker.DraftRetentionWorker(), configured(config)
    for _ in range(4):
        assert instance.tick_with_health(conn, cfg)['scan_cycle_needs_attention']
        assert conn.execute("SELECT status FROM service_state WHERE component='draft_retention'").fetchone()[0] == 'degraded'
    conn.execute('UPDATE knowledge_authoring_drafts SET metadata_json=? WHERE candidate_id=?', (rows[0][1], rows[0][0]))
    instance = worker.DraftRetentionWorker()  # Restart cannot claim recovery after only one good row.
    assert instance.tick_with_health(conn, cfg)['scan_cycle_needs_attention']
    assert not instance.tick_with_health(conn, cfg)['scan_cycle_needs_attention']
    assert conn.execute("SELECT status FROM service_state WHERE component='draft_retention'").fetchone()[0] == 'ready'
