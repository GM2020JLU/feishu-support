import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_review import active_config, remote_runner, result_text

from k3_support.broker_execution_instances import register
from k3_support.executors import ExecutionResult
from k3_support.replay_debug import execute_next_debug
from k3_support.replay_snapshot import replay_snapshot
from k3_support.workflow_replay import ReplayBoundaryError


def forbidden(*args, **kwargs):
    pytest.fail('callback must not run')


def test_execution_replay_rejects_live_database(conn, config):
    with pytest.raises(ReplayBoundaryError):
        execute_next_debug(conn, config, executor=forbidden, observer=forbidden,
                           verification_runner=forbidden, reviewer=forbidden,
                           worker_uid=UID, now=NOW, claim_request_id=str(uuid4()))


@pytest.mark.parametrize('failure', ['executor_error', 'revoked'])
def test_interrupted_execution_never_retries_or_observes_success(conn, config, failure):
    queued(conn)
    cfg = active_config(config)
    case = conn.execute('SELECT case_id FROM jobs').fetchone()[0]
    before = conn.serialize()
    calls = []
    with replay_snapshot(cfg.database_path) as snapshot:
        def execute(inputs, heartbeat):
            calls.append('execute')
            if failure == 'executor_error':
                raise RuntimeError('synthetic interruption')
            snapshot.execute("UPDATE cases SET state='takeover'")
            return result_text(case)

        with pytest.raises((RuntimeError, ValueError)):
            execute_next_debug(snapshot, cfg, executor=execute, observer=forbidden,
                verification_runner=forbidden, reviewer=forbidden, worker_uid=UID,
                now=NOW, claim_request_id=str(uuid4()))
        assert calls == ['execute']
        assert snapshot.execute('SELECT count(*) FROM broker_results').fetchone()[0] == 0
        assert snapshot.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    assert conn.serialize() == before


@pytest.mark.parametrize('observation,expected', [
    ('absent', 'unverified'), ('failed', 'execution_failed'),
    ('cleanup', 'board_cleanup_required'), ('success', 'review_pending'),
    ('takeover', 'stale'),
])
def test_queued_execution_through_completion_never_invents_success(
        conn, config, monkeypatch, observation, expected):
    queued(conn)
    cfg = active_config(config)
    case = conn.execute('SELECT case_id FROM jobs').fetchone()[0]
    before = conn.serialize()
    calls = []

    def execute(inputs, heartbeat):
        calls.append('execute')
        assert inputs['job_id'] == 'job-1'
        assert 'lease_token' not in inputs
        heartbeat()
        return result_text(case)

    def observe(db, *, grant_id, claim_request_id):
        calls.append('observe')
        # This must follow report receipt, with the job still running.
        assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'running'
        assert db.execute('SELECT count(*) FROM broker_results').fetchone()[0] == 1
        if observation == 'absent':
            return
        register(db, grant_id=grant_id, claim_request_id=claim_request_id,
                 invocation_id='a' * 32,
                 cgroup_path=f'/system.slice/k3-support-broker-worker@{claim_request_id}.service')
        db.execute('INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)',
                   (grant_id, 'a' * 32, UID, 1, int(observation == 'failed'), NOW.isoformat()))
        if observation == 'cleanup':
            db.execute('INSERT INTO broker_board_cleanup VALUES(?,?,?,?,?)',
                       (grant_id, 'synthetic-session', 'unknown', NOW.isoformat(), NOW.isoformat()))
        if observation == 'takeover':
            db.execute("UPDATE cases SET state='takeover'")

    def review(db, config, *, job_id, verification_runner, reviewer):
        calls.append('review')
        assert job_id == 'job-1'
        assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'succeeded'
        return {'fixture_review': True}

    # Existing test_replay_debug tests the real review entry separately; this
    # test checks when the complete worker protocol is allowed to enter it.
    monkeypatch.setattr('k3_support.replay_debug.review_completed_debug', review)
    with replay_snapshot(cfg.database_path) as snapshot:
        result = execute_next_debug(snapshot, cfg, executor=execute, observer=observe,
            verification_runner=forbidden, reviewer=forbidden, worker_uid=UID,
            now=NOW, claim_request_id=str(uuid4()))
        assert result['completion']['state'] == expected
        assert calls == ['execute', 'observe'] + (['review'] if observation == 'success' else [])
        assert not result['outbox_intentions']
        assert not result['model_quality_verified'] and not result['external_consumers']
        assert snapshot.execute('SELECT state FROM cases').fetchone()[0] != 'resolved'
    assert conn.serialize() == before


def test_queued_execution_enters_real_review_without_resolving_caller(conn, config):
    from k3_support.executors import create_codex_job
    from k3_support.store import create_case

    cfg = active_config(config)
    case, _ = create_case(conn, title='Synthetic boot failure', case_type='bug',
                          severity='P3', confidence=.8)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
    create_codex_job(conn, cfg, case_id=case, repo='u-boot',
        brief='# UNTRUSTED INPUT\nSynthetic\n# FORBIDDEN ACTIONS\nNo push\n# ACCEPTANCE TESTS\nBuild')
    before = conn.serialize()
    calls = []

    def observe(db, *, grant_id, claim_request_id):
        register(db, grant_id=grant_id, claim_request_id=claim_request_id,
                 invocation_id='b' * 32,
                 cgroup_path=f'/system.slice/k3-support-broker-worker@{claim_request_id}.service')
        db.execute('INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)',
                   (grant_id, 'b' * 32, UID, 1, 0, NOW.isoformat()))

    with replay_snapshot(cfg.database_path) as snapshot:
        def reviewer(argv, prompt, timeout):
            calls.append('review')
            review_id = snapshot.execute('SELECT review_id FROM codex_reviews').fetchone()[0]
            version = snapshot.execute('SELECT version FROM cases WHERE case_id=?', (case,)).fetchone()[0]
            return ExecutionResult(argv, 0, json.dumps({
                'decision_id': f'hermes-review-{review_id}', 'case_id': case,
                'expected_case_version': version, 'intent': 'wait', 'confidence': .96,
                'evidence_ids': [], 'reply_draft': None, 'proposed_actions': [],
                'facts': ['Synthetic local checks passed.'], 'inferences': [],
                'unknowns': ['Caller failure has not been reproduced.']}), '')

        result = execute_next_debug(snapshot, cfg,
            executor=lambda inputs, heartbeat: result_text(case), observer=observe,
            verification_runner=remote_runner(), reviewer=reviewer, worker_uid=UID,
            now=datetime.now(UTC), claim_request_id=str(uuid4()))
        assert result['completion']['state'] == 'review_pending'
        assert result['review']['review']['ok'], result['review']['review']
        assert calls == ['review']
        assert result['review']['case']['state'] != 'resolved'
        assert not any(row['channel'] == 'feishu_im' for row in result['outbox_intentions'])
    assert conn.serialize() == before
