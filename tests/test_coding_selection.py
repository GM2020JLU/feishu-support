import json
from dataclasses import replace
from uuid import uuid4

import pytest
from test_broker_claim import queued
from test_broker_execution_contract import contract, read_contract
from test_broker_start import NOW
from test_review import active_config

from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_input import read
from k3_support.broker_start import authorize
from k3_support.broker_worker import run_one
from k3_support.executors import ExecutorError, create_codex_job, run_codex_job
from k3_support.ids import canonical_json, digest
from k3_support.store import create_case


@pytest.mark.parametrize('agent,wire_api', [('codex', 'responses'), ('claude', 'messages'),
                                          ('dsh', 'chat_completions'), ('opencode', 'responses'),
                                          ('hermes', 'chat_completions')])
def test_version_two_contract_binds_agent_and_custom_model(tmp_path, agent, wire_api):
    value = {**contract(), 'version': 2, 'agent': agent, 'model': 'bound-model',
             'reasoning': 'high', 'wire_api': wire_api}
    first = read_contract(tmp_path, value)
    assert first.selection() == {'agent': agent, 'contract_fingerprint': digest(value)}
    second = read_contract(tmp_path, {**value, 'model': 'different-model'})
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize('change', [{'agent': 'shell'}, {'agent': []}, {'model': '--injected'},
                                    {'model': 'a\nsecret'}, {'reasoning': 'x y'}, {'wire_api': []},
                                    {'agent': 'claude', 'wire_api': 'responses'}])
def test_invalid_version_two_contract_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        read_contract(tmp_path, {**contract(), 'version': 2, 'agent': 'codex', **change})


def test_creation_binds_tool_and_contract_and_legacy_runner_cannot_launch(conn, config):
    cfg = active_config(config)
    case, _ = create_case(conn, title='fixture', case_type='investigation', severity='P2', confidence=.9)
    policy = ExecutionContract('fixture', 'https://example.com', 'bound-model', 'high',
                               'chat_completions', 'a' * 64, agent='dsh')
    options = {'case_id': case, 'repo': 'u-boot',
               'brief': 'UNTRUSTED INPUT\nFORBIDDEN ACTIONS\nACCEPTANCE TESTS'}
    first, created = create_codex_job(conn, cfg, **options, execution_contract=policy)
    assert created
    same, created = create_codex_job(conn, cfg, **options, execution_contract=policy)
    assert same == first and not created
    other, created = create_codex_job(conn, cfg, **options,
                                     execution_contract=replace(policy, agent='hermes', fingerprint='b' * 64))
    assert other != first and created
    row = conn.execute('SELECT * FROM broker_inputs WHERE job_id=?', (first,)).fetchone()
    payload = json.loads(row['payload_json'])
    assert payload['model'] == 'bound-model' and payload['reasoning'] == 'high'
    assert payload['context_extra']['execution'] == policy.selection()
    with pytest.raises(ExecutorError, match='broker worker'):
        run_codex_job(conn, cfg, job_id=first, runner=lambda *_: pytest.fail('must not launch'))
    with pytest.raises(ExecutorError, match='fixed field'):
        create_codex_job(conn, cfg, **options, context_extra={'execution': policy.selection()})


def setup_selection(conn, config):
    queued(conn)
    policy = ExecutionContract('fixture', 'https://example.com', 'gpt-5.6-sol', 'medium',
                               'responses', 'a' * 64, agent='dsh')
    payload = json.loads(conn.execute('SELECT payload_json FROM broker_inputs').fetchone()[0])
    payload['context_extra']['execution'] = policy.selection()
    conn.execute('UPDATE broker_inputs SET payload_json=?', (canonical_json(payload),))
    conn.execute('UPDATE jobs SET input_digest=?', (digest(payload),))
    response = claim(conn, active_config(config), {'version': 1, 'request_id': str(uuid4()),
                     'method': 'claim', 'params': {'pool': 'debug'}},
                     peer_uid=1234, control_key=b't' * 32, now=NOW, contract_reader=lambda: policy)
    return policy, response['task']


@pytest.mark.parametrize('change', [None, {'agent': 'hermes'}, {'model': 'different'},
                                    {'reasoning': 'high'}, {'fingerprint': 'b' * 64}])
def test_control_checks_task_binding_before_recording_start(conn, config, change):
    policy, task = setup_selection(conn, config)
    current = replace(policy, **change) if change else None
    request = {'version': 1, 'request_id': str(uuid4()), 'method': 'start', 'params': {
        **task, **({'contract_fingerprint': current.fingerprint} if current else {})}}
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        authorize(conn, active_config(config), request, peer_uid=1234, now=NOW,
                  contract_reader=(lambda: current) if current else None)
    assert list(conn.iterdump()) == before


def test_selected_task_start_records_exact_contract(conn, config):
    policy, task = setup_selection(conn, config)
    request = {'version': 1, 'request_id': str(uuid4()), 'method': 'start',
               'params': {**task, 'contract_fingerprint': policy.fingerprint}}
    assert authorize(conn, active_config(config), request, peer_uid=1234, now=NOW,
                     contract_reader=lambda: policy)['accepted']
    assert conn.execute('SELECT contract_digest FROM broker_execution_contracts').fetchone()[0] == policy.fingerprint


@pytest.mark.parametrize('agent,fingerprint', [('codex', 'a' * 64), ('dsh', None), ('dsh', 'b' * 64)])
def test_worker_rejects_mismatch_before_preparation_and_start(conn, config, agent, fingerprint):
    _, task = setup_selection(conn, config)
    methods = []
    def transport(request):
        methods.append(request['method'])
        result = {'task': task} if request['method'] == 'claim' else read(conn, request, peer_uid=1234, now=NOW)
        return {'version': 1, 'request_id': request['request_id'], 'ok': True, 'result': result}
    with pytest.raises(ValueError, match='differs'):
        run_one(claim_request_id=str(uuid4()), transport=transport, executor_agent=agent,
                contract_fingerprint=fingerprint, prepare_executor=lambda *_args, **_kwargs: pytest.fail('must not prepare'))
    assert methods == ['claim', 'input']


def test_claim_filters_tool_before_limit_and_preserves_other_tools_queue(conn, config):
    import os

    from k3_support.broker_claim import claim_next

    cfg = active_config(config)
    case, _ = create_case(conn, title='fixture', case_type='investigation', severity='P2', confidence=.9)
    conn.execute("UPDATE cases SET state='investigating' WHERE case_id=?", (case,))
    policy = ExecutionContract('fixture', 'https://example.com', 'bound-model', 'high',
                               'messages', 'a' * 64, agent='claude')
    options = {'case_id': case, 'repo': 'u-boot',
               'brief': 'UNTRUSTED INPUT\nFORBIDDEN ACTIONS\nACCEPTANCE TESTS'}
    for number in range(40):
        create_codex_job(conn, cfg, **options,
                         execution_contract=replace(policy, agent='dsh', fingerprint=f'{number:064x}'))
    target, _ = create_codex_job(conn, cfg, **options, execution_contract=policy)
    assert claim_next(conn, cfg, worker_uid=os.geteuid()+1) is None
    selected = claim_next(conn, cfg, worker_uid=os.geteuid()+1, contract=policy)
    assert selected['job_id'] == target
    assert conn.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0] == 40
