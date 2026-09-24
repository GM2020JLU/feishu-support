from contextlib import contextmanager

import pytest
from test_routing import active_config, route_value
from test_workflow_replay import group_event

from k3_support import executors
from k3_support.job_failure import fail_attempt
from k3_support.orchestrator import continue_failed_retrieval
from k3_support.replay_snapshot import replay_snapshot
from k3_support.store import claim_jobs
from k3_support.workflow_replay import replay_inbound


@pytest.mark.parametrize('superseded', [False, True])
def test_failure_cannot_overwrite_new_attempt(conn, config, superseded):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        replay_inbound(snapshot, cfg, group_event(1, '启动失败'), message_router=lambda _: route_value('research'))
        job = claim_jobs(snapshot, 'test-worker', job_types=('retrieve',))[0]
        if superseded:
            snapshot.execute('UPDATE jobs SET attempt_no=attempt_no+1 WHERE job_id=?', (job['job_id'],))
        changed = fail_attempt(snapshot, job_id=job['job_id'], attempt_no=job['attempt_no'], error_class='TimeoutError')
        assert changed is not superseded
        row = snapshot.execute('SELECT state,lease_owner FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
        assert row['state'] == ('running' if superseded else 'failed')
        assert row['lease_owner'] == ('test-worker' if superseded else None)


@pytest.mark.parametrize('codex', [False, True])
def test_old_failure_cannot_handoff_new_attempt(conn, config, codex):
    cfg = active_config(config)
    cfg.raw['features']['codex'] = codex
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        replay_inbound(snapshot, cfg, group_event(1, '启动失败'), message_router=lambda _: route_value('research'))
        job = claim_jobs(snapshot, 'test-worker', job_types=('retrieve',))[0]
        snapshot.execute('UPDATE jobs SET attempt_no=attempt_no+1 WHERE job_id=?', (job['job_id'],))
        before = list(snapshot.iterdump())
        assert continue_failed_retrieval(snapshot, cfg, job_id=job['job_id'],
            expected_attempt_no=job['attempt_no'], error_class='TimeoutError') is None
        assert list(snapshot.iterdump()) == before


def test_retry_at_codex_insert_boundary_rejects_old_failure(conn, config, monkeypatch):
    cfg = active_config(config)
    cfg.raw['features']['codex'] = True
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        replay_inbound(snapshot, cfg, group_event(1, '启动失败'), message_router=lambda _: route_value('research'))
        job = claim_jobs(snapshot, 'test-worker', job_types=('retrieve',))[0]
        real_transaction = executors.transaction

        @contextmanager
        def retry_before_write(db):
            db.execute('UPDATE jobs SET attempt_no=attempt_no+1 WHERE job_id=?', (job['job_id'],))
            with real_transaction(db):
                yield db

        monkeypatch.setattr(executors, 'transaction', retry_before_write)
        with pytest.raises(executors.ExecutorError, match='retrieval input changed'):
            executors.create_codex_job(snapshot, cfg, case_id=job['case_id'],
                brief='UNTRUSTED INPUT\nFORBIDDEN ACTIONS\nACCEPTANCE TESTS',
                repo='u-boot', retrieval_parent_id=job['job_id'],
                retrieval_attempt_no=job['attempt_no'])
        assert snapshot.execute("SELECT count(*) FROM jobs WHERE job_type='codex'").fetchone()[0] == 0


@pytest.mark.parametrize('superseded', [False, True])
def test_failure_continuation_suppresses_only_authority_race(conn, config, monkeypatch, superseded):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        replay_inbound(snapshot, cfg, group_event(1, '启动失败'), message_router=lambda _: route_value('research'))
        job = claim_jobs(snapshot, 'test-worker', job_types=('retrieve',))[0]

        def fail(*args, **kwargs):
            error = executors.RetrievalHandoffSuperseded if superseded else executors.ExecutorError
            raise error('test failure')

        monkeypatch.setattr('k3_support.orchestrator.continue_research_with_codex', fail)
        kwargs = {'job_id': job['job_id'], 'expected_attempt_no': job['attempt_no'], 'error_class': 'TimeoutError'}
        if superseded:
            assert continue_failed_retrieval(snapshot, cfg, **kwargs) is None
        else:
            with pytest.raises(executors.ExecutorError, match='test failure'):
                continue_failed_retrieval(snapshot, cfg, **kwargs)
