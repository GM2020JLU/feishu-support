import json
import re

import pytest
from test_replay_debug import capture
from test_review import remote_runner

from k3_support import replay_debug_snapshot as replay, replay_history
from k3_support.replay_snapshot import replay_snapshot
from k3_support.review import prepare_codex_review


def request(conn, config):
    cfg, case, job = capture(conn, config)
    rows = []
    fixture = remote_runner()

    def record(argv, cwd, timeout):
        value = fixture(argv, cwd, timeout)
        rows.append(dict(argv=argv, cwd=cwd, timeout=timeout, returncode=value.returncode,
                         stdout=value.stdout, stderr=value.stderr))
        return value

    with replay_snapshot(cfg.database_path) as snapshot:
        prepare_codex_review(snapshot, cfg, job_id=job, runner=record)
    return {'config': cfg.raw, 'job_id': job, 'transcript': rows}


def local_executor(monkeypatch):
    def process(**kwargs):
        assert 'replay_debug_snapshot import execute' in kwargs['argv'][-1]
        return json.dumps(replay.execute(json.loads(kwargs['stdin']),
                          f'/proc/self/fd/{kwargs["pass_fds"][0]}'))
    monkeypatch.setattr(replay_history, 'run_process', process)


def wait_decision(value):
    context = json.loads(value['prompt'].split('REVIEW INPUT JSON:\n')[1])
    return json.dumps({'decision_id': re.search(r'hermes-review-crv_[0-9a-f]+', value['prompt'])[0],
        'case_id': context['case']['case_id'], 'expected_case_version': context['case']['version'],
        'intent': 'wait', 'confidence': .96, 'evidence_ids': [], 'reply_draft': None,
        'proposed_actions': [], 'facts': [], 'inferences': [], 'unknowns': ['Caller failure not reproduced.']})


@pytest.mark.parametrize('valid', [True, False])
def test_sealed_debug_review_calls_model_once_and_preserves_source(conn, config, monkeypatch, valid):
    value = request(conn, config)
    local_executor(monkeypatch)
    before, seen = conn.serialize(), []

    def reviewer(observation):
        seen.append(observation)
        return wait_decision(observation) if valid else '{}'

    result = replay.run(config.database_path, value, reviewer=reviewer)
    assert len(seen) == 1
    assert result['result']['review']['ok'] is valid
    assert result['result']['case']['state'] != 'resolved'
    assert not any(row['channel'] == 'feishu_im' for row in result['result']['outbox_intentions'])
    assert result['transcript']['remaining'] == 0
    assert conn.serialize() == before
    assert replay._replay_id_factory.get() is None


def test_sealed_debug_bad_transcript_never_reaches_model(conn, config, monkeypatch):
    value = request(conn, config)
    value['transcript'][0]['argv'] = ['not-the-command']
    local_executor(monkeypatch)
    result = replay.run(config.database_path, value, reviewer=lambda _: pytest.fail('model'))
    assert result['transcript']['failed']
    assert result['calls'] == []


def test_sealed_debug_expected_answer_rejected_before_copy(config, monkeypatch):
    monkeypatch.setattr(replay, '_snapshot_data', lambda _: pytest.fail('copied'))
    with pytest.raises(ValueError):
        replay.run(config.database_path, {'config': {}, 'job_id': 'j', 'transcript': [], 'answer': '{}'},
                   reviewer=lambda _: pytest.fail('model'))


@pytest.mark.parametrize('exit_status', [None, 1, 0])
def test_sealed_queued_debug_uses_finite_execution_fixture(conn, config, monkeypatch, exit_status):
    from test_review import result_text

    value = request(conn, config)
    case = conn.execute('SELECT case_id FROM jobs WHERE job_id=?', (value['job_id'],)).fetchone()[0]
    conn.execute("UPDATE jobs SET state='queued',available_at='2020-01-01T00:00:00+00:00' WHERE job_id=?",
                 (value['job_id'],))
    value['execution'] = {'report': result_text(case), 'exit_status': exit_status}
    local_executor(monkeypatch)
    before, calls = conn.serialize(), []

    def reviewer(observation):
        calls.append(observation)
        return wait_decision(observation)

    result = replay.run(config.database_path, value, reviewer=reviewer)
    assert result['scope'] == 'sealed_simulated_debug_lifecycle_no_consumers'
    assert result['result']['completion']['state'] == {
        None: 'unverified', 1: 'execution_failed', 0: 'review_pending'}[exit_status]
    assert len(calls) == int(exit_status == 0)
    if exit_status == 0:
        assert result['result']['review']['review']['ok']
        assert result['result']['review']['case']['state'] != 'resolved'
    assert conn.serialize() == before


@pytest.mark.parametrize('fixture', [
    {}, {'report': 'invalid', 'exit_status': 0},
    {'report': 'invalid', 'exit_status': True},
    {'report': 'invalid', 'exit_status': 256},
    {'report': 'invalid', 'exit_status': 0, 'command': ['sh']},
])
def test_invalid_execution_fixture_rejected_before_snapshot(config, monkeypatch, fixture):
    monkeypatch.setattr(replay, '_snapshot_data', lambda _: pytest.fail('copied'))
    with pytest.raises(ValueError):
        replay.run(config.database_path,
                   {'config': {}, 'job_id': 'j', 'transcript': [], 'execution': fixture},
                   reviewer=lambda _: pytest.fail('model'))

