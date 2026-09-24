"""A local non-reproduction report is not authority to close the caller's issue."""
import json
from pathlib import Path

from test_review import active_config, make_job, remote_runner, result_text

from k3_support.executors import ExecutionResult
from k3_support.review import (
    handle_codex_completion,
    prepare_codex_review,
    run_hermes_review,
)


def test_unverified_normal_board_report_stays_open_and_does_not_reply(conn, config):
    cfg = active_config(config)
    case_id, job_id = make_job(conn, cfg)
    job = conn.execute('SELECT workdir FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    text = result_text(case_id).replace('Verified test root cause.',
        'Root cause unknown. board1 booted normally; caller environment has not been reproduced.')
    Path(job['workdir'], 'codex-final.md').write_text(text)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append('verification')
        raise RuntimeError('synthetic evidence source unavailable')

    def no_model(*args, **kwargs):
        raise AssertionError('unverified result must not advance to model review')

    outcome = handle_codex_completion(conn, cfg, job_id=job_id,
                                      remote_runner=unavailable, hermes_runner=no_model)
    assert not outcome['ok']
    assert calls == ['verification']
    case = conn.execute('SELECT state,next_action FROM cases WHERE case_id=?', (case_id,)).fetchone()
    assert case['state'] != 'resolved'
    assert 'owner review required' in case['next_action']
    assert not conn.execute("SELECT 1 FROM outbox WHERE case_id=? AND channel='feishu_im'", (case_id,)).fetchone()
    assert conn.execute("SELECT 1 FROM outbox WHERE case_id=? AND action_type='review_failed'", (case_id,)).fetchone()


def test_verified_build_wait_handoff_preserves_unknown_field_state(conn, config):
    cfg = active_config(config)
    case_id, job_id = make_job(conn, cfg)
    bundle = prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    version = conn.execute('SELECT version FROM cases WHERE case_id=?', (case_id,)).fetchone()[0]

    def reviewer(argv, prompt, timeout):
        assert 'failure to reproduce on a test board is not proof' in prompt
        assert 'Do not send an automatic' in prompt
        decision = {
            'decision_id': f"hermes-review-{bundle['review_id']}", 'case_id': case_id,
            'expected_case_version': version, 'intent': 'wait', 'confidence': .96,
            'evidence_ids': [], 'reply_draft': None, 'proposed_actions': [],
            'facts': ['Independent static and build checks passed.'],
            'inferences': [], 'unknowns': ['Caller firmware version and reproduction environment remain unknown.'],
        }
        return ExecutionResult(argv, 0, json.dumps(decision), '')

    result = run_hermes_review(conn, cfg, job_id=job_id,
                              remote_runner=remote_runner(), hermes_runner=reviewer)
    assert result['applied']['applied']
    assert conn.execute('SELECT state FROM cases WHERE case_id=?', (case_id,)).fetchone()[0] != 'resolved'
    assert not conn.execute("SELECT 1 FROM outbox WHERE case_id=? AND channel='feishu_im'", (case_id,)).fetchone()
    notice = conn.execute("SELECT payload_json FROM outbox WHERE case_id=? AND channel='telegram'", (case_id,)).fetchone()
    assert notice and 'Caller firmware version' in notice[0]
    assert '不等于对方现场已解决' in json.loads(notice[0])['text']


def test_model_cannot_close_case_after_local_success(conn, config):
    cfg = active_config(config)
    case_id, job_id = make_job(conn, cfg)
    bundle = prepare_codex_review(conn, cfg, job_id=job_id, runner=remote_runner())
    version = conn.execute('SELECT version FROM cases WHERE case_id=?', (case_id,)).fetchone()[0]
    calls = []

    def reviewer(argv, prompt, timeout):
        calls.append(prompt)
        decision = {
            'decision_id': f"hermes-review-{bundle['review_id']}", 'case_id': case_id,
            'expected_case_version': version, 'intent': 'resolve', 'confidence': .99,
            'evidence_ids': [], 'reply_draft': None, 'proposed_actions': [],
            'facts': ['Local test passed.'], 'inferences': ['Caller issue is fixed.'], 'unknowns': [],
        }
        return ExecutionResult(argv, 0, json.dumps(decision), '')

    outcome = handle_codex_completion(conn, cfg, job_id=job_id,
        remote_runner=remote_runner(), hermes_runner=reviewer)
    assert not outcome['ok']
    assert len(calls) == 2
    assert conn.execute('SELECT state FROM cases WHERE case_id=?', (case_id,)).fetchone()[0] != 'resolved'
    assert not conn.execute("SELECT 1 FROM outbox WHERE case_id=? AND channel='feishu_im'", (case_id,)).fetchone()
    assert conn.execute("SELECT 1 FROM outbox WHERE case_id=? AND action_type='review_failed'", (case_id,)).fetchone()
