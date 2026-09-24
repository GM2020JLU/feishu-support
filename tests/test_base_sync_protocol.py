"""Synthetic Base: assert actual transport ordering, not just job state."""

import json

import pytest
from test_mail_calendar_base import configured

from k3_support.base_sync import enqueue_dirty_entities, run_base_sync_job, fail_base_sync_job, sync_case
from k3_support.base_sync_attempt import AttemptRef
from k3_support.base_sync_protocol import BaseSyncBusy, BaseSyncInputChanged, BaseSyncSuperseded
from k3_support.lark import CommandResult
from k3_support.store import create_case, claim_jobs, recover_stale_jobs


def setup(conn, config):
    cfg = configured(config, base_sync=True)
    case, _ = create_case(conn, title='synthetic', case_type='bug', severity='P3', confidence=0.8)
    enqueue_dirty_entities(conn, cfg)
    job = claim_jobs(conn, 'old-worker', job_types=('base_sync',))[0]
    return cfg, case, job, AttemptRef.from_claim(job)


def reply(argv):
    return CommandResult({'record_id_list': ['rec_synthetic']} if '+record-batch-create' in argv else {}, 'user', [])


def succeed(conn, cfg, job, ref, runner=reply):
    return run_base_sync_job(conn, cfg, job_id=job['job_id'], attempt_ref=ref, runner=runner)


def replace_attempt(conn, job):
    conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job['job_id'],))
    recover_stale_jobs(conn)
    return claim_jobs(conn, 'new-worker', job_types=('base_sync',))[0]


@pytest.mark.parametrize('during', ['search', 'write'])
@pytest.mark.parametrize('outcome', ['success', 'failure'])
def test_late_transport_never_commits_successor_or_mapping(conn, config, during, outcome):
    cfg, case, job, ref = setup(conn, config)
    successor, calls = [], []
    def runner(argv):
        assert not conn.in_transaction
        calls.append(argv[1])
        if (during == 'search' and argv[1] == '+record-search') or (during == 'write' and argv[1] == '+record-batch-create'):
            successor.append(replace_attempt(conn, job))
            if outcome == 'failure':
                raise RuntimeError('late transport failure')
        return reply(argv)
    try:
        result = succeed(conn, cfg, job, ref, runner)
        assert result['superseded']
    except (RuntimeError, BaseSyncSuperseded) as error:
        failure = fail_base_sync_job(conn, job_id=job['job_id'], attempt_ref=ref, error=error)
        assert failure['state'] == 'superseded'
    assert tuple(conn.execute('SELECT state,lease_owner,attempt_no FROM jobs').fetchone()) == ('running', 'new-worker', 2)
    assert conn.execute('SELECT count(*) FROM base_mappings').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM job_attempts WHERE worker_id=\'new-worker\'').fetchone()[0] == 0
    assert calls.count('+record-batch-create') == int(during == 'write')
    operation = conn.execute('SELECT state,result_json FROM base_sync_operations').fetchone()
    expected = 'cancelled' if during == 'search' else 'settled' if outcome == 'success' else 'unknown'
    assert operation['state'] == expected
    newer = successor[0]
    if expected == 'unknown':
        with pytest.raises(BaseSyncBusy):
            succeed(conn, cfg, newer, AttemptRef.from_claim(newer), lambda _: pytest.fail('unknown must not resend'))
    elif expected == 'settled':
        sent = []
        def update(argv):
            sent.append(argv[1])
            return reply(argv)
        succeed(conn, cfg, newer, AttemptRef.from_claim(newer), update)
        assert sent == ['+record-batch-update']
        assert conn.execute('SELECT record_id FROM base_mappings').fetchone()[0] == 'rec_synthetic'


@pytest.mark.parametrize('failure', ['lost_response', 'bad_create_receipt'])
def test_unknown_remote_write_is_durable_and_blocks_direct_and_worker(conn, config, failure):
    cfg, case, job, ref = setup(conn, config)
    def runner(argv):
        if '+record-batch-create' in argv:
            if failure == 'lost_response':
                raise RuntimeError('lost response')
            return CommandResult({}, 'user', [])
        return reply(argv)
    with pytest.raises(RuntimeError) as caught:
        succeed(conn, cfg, job, ref, runner)
    state = fail_base_sync_job(conn, job_id=job['job_id'], attempt_ref=ref, error=caught.value)
    assert state['state'] == 'waiting' and not state['retry']
    conn.execute("UPDATE base_sync_operations SET expires_at='2000-01-01T00:00:00+00:00'")
    with pytest.raises(BaseSyncBusy):
        sync_case(conn, cfg, case_id=case, runner=lambda _: pytest.fail('must not retry'))
    assert conn.execute('SELECT state FROM base_sync_operations').fetchone()[0] == 'unknown'


def test_original_claim_lost_before_call_cannot_adopt_successor(conn, config):
    cfg, case, job, ref = setup(conn, config)
    replace_attempt(conn, job)
    with pytest.raises(BaseSyncSuperseded):
        succeed(conn, cfg, job, ref, lambda _: pytest.fail('stale attempt'))
    assert conn.execute('SELECT count(*) FROM base_sync_operations').fetchone()[0] == 0


def test_snapshot_change_during_search_never_dispatches_old_payload(conn, config):
    cfg, case, job, ref = setup(conn, config)
    def runner(argv):
        assert argv[1] == '+record-search'
        conn.execute("UPDATE cases SET title='new title',version=version+1 WHERE case_id=?", (case,))
        return reply(argv)
    with pytest.raises(BaseSyncInputChanged) as caught:
        succeed(conn, cfg, job, ref, runner)
    assert fail_base_sync_job(conn, job_id=job['job_id'], attempt_ref=ref, error=caught.value)['state'] == 'cancelled'
    assert conn.execute('SELECT input_digest FROM jobs').fetchone()[0] == ref.input_digest
    assert enqueue_dirty_entities(conn, cfg)['queued'] == 1


def test_direct_competitor_cannot_prepare_or_write_while_worker_owns_entity(conn, config):
    cfg, case, job, ref = setup(conn, config)
    calls = []
    def runner(argv):
        calls.append(argv[1])
        with pytest.raises(BaseSyncBusy):
            sync_case(conn, cfg, case_id=case, runner=lambda _: pytest.fail('overlapping write'))
        return reply(argv)
    succeed(conn, cfg, job, ref, runner)
    assert calls == ['+record-search', '+record-batch-create']


def test_crash_after_remote_success_before_local_commit_holds_slot(conn, config, monkeypatch):
    from k3_support import base_sync_protocol
    cfg, case, job, ref = setup(conn, config)
    def crash(*args, **kwargs):
        raise RuntimeError('injected crash before commit')
    monkeypatch.setattr(base_sync_protocol, '_finish', crash)
    with pytest.raises(RuntimeError, match='before commit'):
        succeed(conn, cfg, job, ref)
    assert conn.execute('SELECT state FROM base_sync_operations').fetchone()[0] == 'unknown'
    assert conn.execute('SELECT count(*) FROM base_mappings').fetchone()[0] == 0
    with pytest.raises(BaseSyncBusy):
        sync_case(conn, cfg, case_id=case, runner=lambda _: pytest.fail('unknown remote result'))


def test_mapping_records_sent_version_not_changes_during_request(conn, config):
    cfg, case, job, ref = setup(conn, config)
    original = conn.execute('SELECT version FROM cases').fetchone()[0]
    def runner(argv):
        if '+record-batch-create' in argv:
            conn.execute("UPDATE cases SET title='later',version=version+1 WHERE case_id=?", (case,))
        return reply(argv)
    result = succeed(conn, cfg, job, ref, runner)
    assert not result['superseded']
    assert conn.execute('SELECT mirrored_version FROM base_mappings').fetchone()[0] == original
    assert enqueue_dirty_entities(conn, cfg)['queued'] == 1


def test_concurrent_connections_allow_only_one_actual_remote_write(conn, config):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from k3_support.db import connect

    cfg, case, job, ref = setup(conn, config)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def first():
        other = connect(config.database_path)
        try:
            def runner(argv):
                calls.append(argv[1])
                if '+record-batch-create' in argv:
                    entered.set()
                    assert release.wait(5)
                return reply(argv)
            return succeed(other, cfg, job, ref, runner)
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert entered.wait(5)
            with pytest.raises(BaseSyncBusy):
                sync_case(conn, cfg, case_id=case, runner=lambda _: pytest.fail('concurrent write'))
        finally:
            release.set()
        assert not future.result(timeout=5)['superseded']
    assert calls.count('+record-batch-create') == 1


def test_delayed_preparation_cannot_overwrite_newer_completed_generation(conn, config):
    from k3_support.base_sync_protocol import _reserve, _prepare, _dispatch
    cfg, case, job, ref = setup(conn, config)
    old = _reserve(conn, cfg, 'case', case, None)
    row, _ = _prepare(conn, old)
    conn.execute("UPDATE base_sync_operations SET expires_at='2000-01-01T00:00:00+00:00' WHERE operation_id=?", (old,))
    conn.execute("UPDATE cases SET title='new generation',version=version+1 WHERE case_id=?", (case,))
    new = sync_case(conn, cfg, case_id=case, runner=reply)
    assert new['version'] > row['target_version']
    with pytest.raises(BaseSyncSuperseded):
        _dispatch(conn, cfg, old, None, {'create_records': [json.loads(row['fields_json'])]})
    assert conn.execute('SELECT mirrored_version FROM base_mappings').fetchone()[0] == new['version']


def test_job_mapping_and_attempt_receipt_commit_atomically(conn, config):
    cfg, case, job, ref = setup(conn, config)
    def runner(argv):
        if '+record-batch-create' in argv:
            conn.execute('DELETE FROM job_attempts WHERE job_id=?', (job['job_id'],))
        return reply(argv)
    with pytest.raises(BaseSyncSuperseded, match='completion record'):
        succeed(conn, cfg, job, ref, runner)
    assert conn.execute('SELECT count(*) FROM base_mappings').fetchone()[0] == 0
    assert conn.execute('SELECT state FROM jobs').fetchone()[0] == 'running'
    assert conn.execute('SELECT state FROM base_sync_operations').fetchone()[0] == 'unknown'


def test_expired_lease_at_response_does_not_commit_even_without_successor(conn, config):
    cfg, case, job, ref = setup(conn, config)
    def runner(argv):
        if '+record-batch-create' in argv:
            conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00'")
        return reply(argv)
    assert succeed(conn, cfg, job, ref, runner)['superseded']
    assert conn.execute('SELECT count(*) FROM base_mappings').fetchone()[0] == 0
    assert conn.execute('SELECT state FROM jobs').fetchone()[0] == 'running'
    assert conn.execute('SELECT state FROM base_sync_operations').fetchone()[0] == 'settled'


def test_destination_change_has_distinct_input_and_rechecks_before_dispatch(conn, config):
    cfg, case, job, ref = setup(conn, config)
    def runner(argv):
        assert argv[1] == '+record-search'
        cfg.raw['base']['app_token'] = 'another-synthetic-base'
        return reply(argv)
    with pytest.raises(BaseSyncInputChanged, match='destination'):
        succeed(conn, cfg, job, ref, runner)
    assert enqueue_dirty_entities(conn, cfg)['queued'] == 1
    rows = conn.execute('SELECT DISTINCT input_digest FROM jobs').fetchall()
    assert len(rows) == 2


def test_unknown_survives_reopened_connection_and_readonly_report(conn, config):
    from k3_support.db import connect
    from k3_support.base_sync_inventory import snapshot
    cfg, case, job, ref = setup(conn, config)
    def runner(argv):
        if '+record-batch-create' in argv:
            raise RuntimeError('lost response')
        return reply(argv)
    with pytest.raises(RuntimeError):
        succeed(conn, cfg, job, ref, runner)
    reopened = connect(config.database_path)
    try:
        before = list(reopened.iterdump())
        report = snapshot(reopened)
        assert report['operations'][0]['state'] == 'unknown'
        assert not report['remote_completion_verified']
        assert 'fields_json' not in report['operations'][0]
        assert list(reopened.iterdump()) == before
        with pytest.raises(BaseSyncBusy):
            sync_case(reopened, cfg, case_id=case, runner=lambda _: pytest.fail('restart resend'))
    finally:
        reopened.close()
