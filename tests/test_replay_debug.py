import hashlib
import json

import pytest
from test_broker_grant_schema import grant
from test_review import active_config, make_job, remote_runner, result_text

from k3_support.executors import ExecutionResult
from k3_support.replay_debug import review_completed_debug
from k3_support.replay_snapshot import replay_snapshot
from k3_support.workflow_replay import ReplayBoundaryError


def capture(conn, config):
    cfg = active_config(config)
    case, job = make_job(conn, cfg)
    conn.execute('UPDATE jobs SET attempt_no=1 WHERE job_id=?', (job,))
    row = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job,)).fetchone()
    grant(conn, job_id=job, input_digest=row['input_digest'])
    text = result_text(case)
    conn.execute('INSERT INTO broker_results VALUES(?,?,?,?,?,?,?,?,?)',
        ('grant-1', job, 1, 1, row['input_digest'], hashlib.sha256(text.encode()).hexdigest(), text, '{}', 'now'))
    return cfg, case, job


def test_debug_replay_rejects_live_db_before_any_callback(conn, config):
    with pytest.raises(ReplayBoundaryError):
        review_completed_debug(conn, config, job_id='x',
            verification_runner=lambda *a: pytest.fail('verification'), reviewer=lambda *a: pytest.fail('model'))


def test_debug_replay_preserves_unknown_caller_and_source(conn, config):
    cfg, case, job = capture(conn, config)
    before = conn.serialize()
    calls = []
    with replay_snapshot(cfg.database_path) as snapshot:
        def reviewer(argv, prompt, timeout):
            calls.append(prompt)
            review = snapshot.execute('SELECT review_id FROM codex_reviews WHERE job_id=?', (job,)).fetchone()[0]
            version = snapshot.execute('SELECT version FROM cases WHERE case_id=?', (case,)).fetchone()[0]
            return ExecutionResult(argv, 0, json.dumps({
                'decision_id': f'hermes-review-{review}', 'case_id': case, 'expected_case_version': version,
                'intent': 'wait', 'confidence': .96, 'evidence_ids': [], 'reply_draft': None,
                'proposed_actions': [], 'facts': ['Local checks passed.'], 'inferences': [],
                'unknowns': ['Caller failure remains unreproduced.']}), '')
        result = review_completed_debug(snapshot, cfg, job_id=job,
            verification_runner=remote_runner(), reviewer=reviewer)
        assert result['review']['ok']
        assert len(calls) == 1
        assert result['case']['state'] != 'resolved'
        assert not any(row['channel'] == 'feishu_im' for row in result['outbox_intentions'])
    assert conn.serialize() == before


@pytest.mark.parametrize('change', ["UPDATE broker_results SET result_text='changed'", 'DELETE FROM broker_grants'])
def test_debug_replay_no_file_fallback(conn, config, change):
    cfg, case, job = capture(conn, config)
    with replay_snapshot(cfg.database_path) as snapshot:
        if change.startswith('DELETE'):
            snapshot.execute('DELETE FROM broker_results')
        snapshot.execute(change)
        with pytest.raises((ValueError, ReplayBoundaryError)):
            review_completed_debug(snapshot, cfg, job_id=job,
                verification_runner=lambda *a: pytest.fail('verification'), reviewer=lambda *a: pytest.fail('model'))


@pytest.mark.parametrize('state', ['takeover', 'paused', 'resolved', 'cancelled'])
def test_debug_replay_respects_owner_control_before_review(conn, config, state):
    cfg, case, job = capture(conn, config)
    with replay_snapshot(cfg.database_path) as snapshot:
        snapshot.execute('UPDATE cases SET state=? WHERE case_id=?', (state, case))
        result = review_completed_debug(snapshot, cfg, job_id=job,
            verification_runner=lambda *a: pytest.fail('verification'), reviewer=lambda *a: pytest.fail('model'))
        assert result['review']['suppressed']
        assert result['case']['state'] == state
        assert not result['outbox_intentions']


def test_debug_replay_failed_verification_does_not_call_model(conn, config):
    cfg, case, job = capture(conn, config)
    with replay_snapshot(cfg.database_path) as snapshot:
        def unavailable(*args):
            raise RuntimeError('fixture verification unavailable')
        result = review_completed_debug(snapshot, cfg, job_id=job,
            verification_runner=unavailable, reviewer=lambda *a: pytest.fail('model'))
        assert not result['review']['ok']
        assert result['case']['state'] != 'resolved'
        assert any(row['action_type'] == 'review_failed' for row in result['outbox_intentions'])
        assert not any(row['channel'] == 'feishu_im' for row in result['outbox_intentions'])


def test_local_success_cannot_authorize_model_to_resolve_callers_failure(conn, config):
    """A model claiming resolution must fail even after valid local checks."""
    cfg, case, job = capture(conn, config)
    before = conn.serialize()
    calls = []
    with replay_snapshot(cfg.database_path) as snapshot:
        def reviewer(argv, prompt, timeout):
            calls.append(prompt)
            review = snapshot.execute(
                'SELECT review_id FROM codex_reviews WHERE job_id=?', (job,)).fetchone()[0]
            version = snapshot.execute(
                'SELECT version FROM cases WHERE case_id=?', (case,)).fetchone()[0]
            return ExecutionResult(argv, 0, json.dumps({
                'decision_id': f'hermes-review-{review}', 'case_id': case,
                'expected_case_version': version, 'intent': 'resolve',
                'confidence': .99, 'evidence_ids': [], 'reply_draft': None,
                'proposed_actions': [], 'facts': ['Local checks passed.'],
                'inferences': ['Therefore the caller failure is fixed.'],
                'unknowns': []}), '')

        result = review_completed_debug(snapshot, cfg, job_id=job,
            verification_runner=remote_runner(), reviewer=reviewer)
        assert not result['review']['ok']
        assert len(calls) == 2  # Existing bounded correction, never an open retry loop.
        assert result['case']['state'] != 'resolved'
        assert any(row['action_type'] == 'review_failed' for row in result['outbox_intentions'])
        assert not any(row['channel'] == 'feishu_im' for row in result['outbox_intentions'])
    assert conn.serialize() == before


def test_debug_verification_replays_exact_finite_transcript(conn, config):
    from k3_support.review import prepare_codex_review
    from k3_support.replay_transcript import VerificationTranscript
    cfg, case, job = capture(conn, config)
    records = []
    fixture = remote_runner()

    def record(argv, cwd, timeout):
        response = fixture(argv, cwd, timeout)
        records.append(dict(argv=argv, cwd=cwd, timeout=timeout, returncode=response.returncode,
                            stdout=response.stdout, stderr=response.stderr))
        return response

    before = conn.serialize()
    with replay_snapshot(cfg.database_path) as snapshot:
        prepare_codex_review(snapshot, cfg, job_id=job, runner=record)
    transcript = VerificationTranscript(records)
    model_calls = []
    with replay_snapshot(cfg.database_path) as snapshot:
        def unavailable(*args):
            model_calls.append(1)
            raise RuntimeError('fixture model unavailable')
        result = review_completed_debug(snapshot, cfg, job_id=job,
            verification_runner=transcript, reviewer=unavailable)
        assert not result['review']['ok']
        assert model_calls
        assert transcript.status()['consumed'] == len(records) > 0
        assert not transcript.status()['failed']
    assert conn.serialize() == before
