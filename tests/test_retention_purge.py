import pytest
from test_retention_recheck import candidate
from k3_support.operations import apply_retention
from k3_support.retention_purge import preview
from k3_support.store import create_case


def test_quarantine_preview_is_readonly_and_checks_new_reference(conn, config):
    _, event, candidates = candidate(conn, config)
    assert apply_retention(conn, config, candidates)['quarantined'] == 1
    attempt = conn.execute('SELECT attempt_id FROM retention_attempts').fetchone()[0]
    assert 'quarantine_grace_period' in preview(conn, config, attempt_id=attempt, days=30)['blockers']
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    before = conn.serialize()
    report = preview(conn, config, attempt_id=attempt, days=30)
    assert report['blockers'] == [] and not report['deletion_authorized']
    assert conn.serialize() == before
    create_case(conn, title='new inquiry', case_type='bug', severity='P3', confidence=.8, source_event_pk=event)
    assert 'referenced' in preview(conn, config, attempt_id=attempt, days=30)['blockers']


@pytest.mark.parametrize('days', [True, 0, 3651, '30'])
def test_invalid_purge_period_is_rejected(conn, config, days):
    with pytest.raises(ValueError):
        preview(conn, config, attempt_id='unknown', days=days)


def test_purge_intent_requires_confirmation_and_exact_idempotent_binding(conn, config):
    from pathlib import Path
    from uuid import uuid4
    from k3_support.retention_purge import prepare
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    row = conn.execute('SELECT * FROM retention_attempts').fetchone()
    shown = preview(conn, config, attempt_id=row['attempt_id'], days=30)
    kwargs = dict(attempt_id=row['attempt_id'], days=30, binding_digest=shown['binding_digest'],
                  request_id=str(uuid4()), actor_id='owner')
    with pytest.raises(ValueError):
        prepare(conn, config, **kwargs)
    assert not conn.execute('SELECT 1 FROM retention_purge_requests').fetchone()
    result = prepare(conn, config, **kwargs, confirm_permanent_delete=True)
    assert result['state'] == 'prepared' and result['files_deleted'] == 0
    assert prepare(conn, config, **kwargs, confirm_permanent_delete=True)['replayed']
    for change in ({'actor_id': 'other'}, {'days': 31}, {'binding_digest': 'wrong'}, {'request_id': str(uuid4())}):
        with pytest.raises(ValueError):
            prepare(conn, config, **{**kwargs, **change}, confirm_permanent_delete=True)
    assert Path(row['quarantine_path']).read_text() == 'retained evidence'
    assert conn.execute('SELECT raw_artifact_path FROM inbound_events').fetchone()[0] == row['quarantine_path']
    assert conn.execute('SELECT count(*) FROM retention_purge_requests').fetchone()[0] == 1


@pytest.mark.parametrize('failure', [False, True])
def test_execute_purge_is_once_and_failed_unlink_blocks_recovery(conn, config, monkeypatch, failure):
    from pathlib import Path
    from uuid import uuid4
    from k3_support import retention_purge as purge
    from k3_support.retention_recovery import recover
    from k3_support.operations import OperationsError
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    row = conn.execute('SELECT * FROM retention_attempts').fetchone()
    shown = preview(conn, config, attempt_id=row['attempt_id'], days=30)
    request = str(uuid4())
    purge.prepare(conn, config, attempt_id=row['attempt_id'], days=30,
                  binding_digest=shown['binding_digest'], request_id=request, actor_id='owner', confirm_permanent_delete=True)
    if failure:
        def fail(*args, **kwargs):
            raise OSError('synthetic unlink failure')
        monkeypatch.setattr(purge.os, 'unlink', fail)
        with pytest.raises(OSError):
            purge.execute(conn, config, request_id=request, actor_id='owner')
        assert Path(row['quarantine_path']).exists()
    else:
        assert purge.execute(conn, config, request_id=request, actor_id='owner')['files_deleted'] == 1
        assert not Path(row['quarantine_path']).exists()
        assert conn.execute('SELECT raw_artifact_path FROM inbound_events').fetchone()[0] is None
    again = purge.execute(conn, config, request_id=request, actor_id='owner')
    assert again['state'] == ('unknown' if failure else 'purged') and again['files_deleted'] == 0
    with pytest.raises(OperationsError, match='purge'):
        recover(conn, config, row['attempt_id'])


@pytest.mark.parametrize('state', ['prepared', 'running', 'unknown', 'purged'])
def test_cancellation_and_restore_fencing_never_restart_purge(conn, config, state):
    from uuid import uuid4
    from k3_support.retention_purge import cancel, execute
    from k3_support.recovery_fence import fence
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    attempt = conn.execute('SELECT attempt_id FROM retention_attempts').fetchone()[0]
    request = str(uuid4())
    conn.execute('INSERT INTO retention_purge_requests VALUES(?,?,?,?,?,?,?,?)',
                 (request, attempt, 'owner', 'fixture-digest', 30, state, 'old', 'old'))
    if state == 'prepared':
        with pytest.raises(ValueError):
            cancel(conn, request_id=request, actor_id='other')
        assert cancel(conn, request_id=request, actor_id='owner')['state'] == 'cancelled'
        assert cancel(conn, request_id=request, actor_id='owner')['replayed']
        conn.execute("UPDATE retention_purge_requests SET state='prepared'")
    else:
        with pytest.raises(ValueError):
            cancel(conn, request_id=request, actor_id='owner')
    fence(conn)
    expected = 'cancelled' if state == 'prepared' else 'unknown' if state == 'running' else state
    assert execute(conn, config, request_id=request, actor_id='owner')['state'] == expected
    assert conn.execute('SELECT raw_artifact_path FROM inbound_events').fetchone()[0] is not None


def test_unlink_before_commit_failure_is_observable_without_retry(conn, config, monkeypatch):
    from uuid import uuid4
    from k3_support import retention_purge as purge
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    attempt = conn.execute('SELECT attempt_id FROM retention_attempts').fetchone()[0]
    shown = preview(conn, config, attempt_id=attempt, days=30)
    request = str(uuid4())
    purge.prepare(conn, config, attempt_id=attempt, days=30, binding_digest=shown['binding_digest'],
                  request_id=request, actor_id='owner', confirm_permanent_delete=True)
    original = purge.inspect(conn, config, request_id=request, actor_id='owner')
    def fail(_fd):
        raise OSError('synthetic fsync failure after unlink')
    monkeypatch.setattr(purge.os, 'fsync', fail)
    with pytest.raises(OSError):
        purge.execute(conn, config, request_id=request, actor_id='owner')
    before = conn.serialize()
    report = purge.inspect(conn, config, request_id=request, actor_id='owner')
    assert report['request_state'] == 'unknown'
    assert report['observations']['quarantine_path']['state'] == 'absent'
    assert report['source_binding'] == 'quarantine'
    assert not report['retry_authorized']
    assert report['observation_digest'] != original['observation_digest']
    assert conn.serialize() == before
    with pytest.raises(ValueError):
        purge.inspect(conn, config, request_id=request, actor_id='other')
    assert purge.execute(conn, config, request_id=request, actor_id='owner')['files_deleted'] == 0
    with pytest.raises(ValueError):
        purge.reconcile(conn, config, request_id=request, actor_id='owner', decision='confirm_absence',
                        observation_digest=report['observation_digest'])
    with pytest.raises(ValueError):
        purge.reconcile(conn, config, request_id=request, actor_id='owner', decision='confirm_absence',
                        observation_digest=original['observation_digest'], confirm=True)
    result = purge.reconcile(conn, config, request_id=request, actor_id='owner', decision='confirm_absence',
                             observation_digest=report['observation_digest'], confirm=True)
    assert result['files_deleted'] == 0
    assert conn.execute('SELECT reason FROM retention_attempts').fetchone()[0] == 'operator_confirmed_absence'
    assert conn.execute('SELECT raw_artifact_path FROM inbound_events').fetchone()[0] is None
    assert purge.reconcile(conn, config, request_id=request, actor_id='owner', decision='confirm_absence',
                           observation_digest=report['observation_digest'], confirm=True)['replayed']


def test_reconcile_existing_file_cancels_without_deleting_and_refuses_busy_lock(conn, config):
    from uuid import uuid4
    from pathlib import Path
    from k3_support import retention_purge as purge
    from k3_support.retention_operation_guard import hold
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    attempt = conn.execute('SELECT * FROM retention_attempts').fetchone()
    request = str(uuid4())
    conn.execute('INSERT INTO retention_purge_requests VALUES(?,?,?,?,?,?,?,?)',
                 (request, attempt['attempt_id'], 'owner', 'fixture', 30, 'unknown', 'old', 'old'))
    observation = purge.inspect(conn, config, request_id=request, actor_id='owner')
    kwargs = dict(request_id=request, actor_id='owner', decision='keep_file',
                  observation_digest=observation['observation_digest'], confirm=True)
    with hold(config):
        with pytest.raises(ValueError, match='still running'):
            purge.reconcile(conn, config, **kwargs)
    assert purge.reconcile(conn, config, **kwargs)['files_deleted'] == 0
    assert conn.execute('SELECT state FROM retention_purge_requests').fetchone()[0] == 'cancelled'
    assert Path(attempt['quarantine_path']).read_text() == 'retained evidence'


def test_expired_confirmation_does_not_start_or_delete(conn, config):
    from pathlib import Path
    from uuid import uuid4
    from k3_support import retention_purge as purge
    _, _, candidates = candidate(conn, config)
    apply_retention(conn, config, candidates)
    conn.execute("UPDATE retention_attempts SET updated_at='2020-01-01T00:00:00+00:00'")
    attempt = conn.execute('SELECT * FROM retention_attempts').fetchone()
    shown = preview(conn, config, attempt_id=attempt['attempt_id'], days=30)
    request = str(uuid4())
    purge.prepare(conn, config, attempt_id=attempt['attempt_id'], days=30, binding_digest=shown['binding_digest'],
                  request_id=request, actor_id='owner', confirm_permanent_delete=True)
    conn.execute("UPDATE retention_purge_requests SET created_at='2020-01-01T00:00:00+00:00'")
    with pytest.raises(ValueError, match='expired'):
        purge.execute(conn, config, request_id=request, actor_id='owner')
    assert Path(attempt['quarantine_path']).exists()
    assert conn.execute('SELECT state FROM retention_purge_requests').fetchone()[0] == 'prepared'
    assert purge.cancel(conn, request_id=request, actor_id='owner')['state'] == 'cancelled'
