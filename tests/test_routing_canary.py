import runpy
from pathlib import Path

import pytest
from test_routing import route_value


def verifier():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/verify-routing-canary.py'))


def test_workflow_canary_uses_production_context_and_preserves_source(monkeypatch):
    from test_replay_inference import local_executor

    local_executor(monkeypatch)
    calls = []

    def transport(identity, *, timeout, prompt):
        calls.append(prompt)
        assert timeout == 45
        assert 'allowed_routes' not in prompt and 'expected_route' not in prompt
        return {'ok': True, 'result': route_value('codex_debug', issue_type='bug',
                reason_codes=['technical_investigation']), 'model': 'synthetic',
                'provider': 'synthetic', 'manifest_digest': 'synthetic'}

    result = verifier()['verify_workflow']({}, transport=transport)
    assert result['ok'] and result['source_unchanged'] and len(calls) == 1
    assert result['proposed_route'] == result['applied_route'] == 'codex_debug'
    assert result['jobs'] == [{'job_type': 'retrieve', 'state': 'queued'}]
    assert not result['workers_executed'] and not result['live_messages_sent']


def test_workflow_canary_does_not_retry_failed_transport(monkeypatch):
    from test_replay_inference import local_executor

    local_executor(monkeypatch)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return {'ok': False}

    with pytest.raises(ValueError, match='inference failed'):
        verifier()['verify_workflow']({}, transport=transport)
    assert calls == [1]


def test_canary_does_not_send_expectations_to_transport():
    module = verifier()
    replies = iter(['direct_answer', 'owner_decision', 'codex_debug'])
    calls = []

    def transport(identity, *, timeout, prompt):
        calls.append(prompt)
        assert 'allowed_routes' not in prompt and 'matched' not in prompt
        assert timeout == 45
        route = next(replies)
        reasons = ['approved_knowledge_match'] if route == 'direct_answer' else ['technical_investigation']
        return {'ok': True, 'result': route_value(route, reason_codes=reasons), 'model': 'synthetic',
                'provider': 'synthetic', 'manifest_digest': 'synthetic'}

    result = module['verify']({}, transport=transport)
    assert result['ok'] and len(calls) == 3
    assert result['human_gold_reviewed'] is False


def test_canary_stops_after_first_transport_failure():
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return {'ok': False, 'stage': 'synthetic failure'}

    result = verifier()['verify']({}, transport=transport)
    assert not result['ok'] and calls == [1]


def test_timeout_preserves_prior_results_without_leaking_or_retrying():
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise TimeoutError('PRIVATE ENDPOINT AND PROMPT')
        return {'ok': True, 'result': route_value('direct_answer', reason_codes=['approved_knowledge_match']),
                'model': 'fixture', 'provider': 'fixture', 'manifest_digest': 'fixture'}

    result = verifier()['verify']({}, transport=transport)
    assert not result['ok'] and len(calls) == 2
    assert result['cases'][0]['matched']
    assert result['cases'][1]['failure_code'] == 'timeout'
    assert 'PRIVATE' not in str(result)


def test_bridge_failure_codes_never_echo_exception_text():
    from k3_support.hermes_stdin import BridgeAbort, BridgeError, failure_code

    assert failure_code(BridgeAbort('tools_exposed')) == 'tools_exposed'
    assert failure_code(BridgeAbort('PRIVATE')) == 'runtime_exit'
    assert failure_code(BridgeError('PRIVATE')) == 'bridge_validation_failed'
    assert failure_code(ValueError('PRIVATE')) == 'runtime_failed'


def test_selected_cases_do_not_repeat_prior_calls():
    calls = []

    def transport(identity, *, timeout, prompt):
        calls.append(prompt)
        assert 'pico的风扇太吵了' not in prompt
        return {'ok': True, 'result': route_value('owner_decision'), 'model': 'fixture',
                'provider': 'fixture', 'manifest_digest': 'fixture'}

    result = verifier()['verify']({}, transport=transport, case_ids=['manager-commitment'])
    assert result['ok'] and result['requested_cases'] == 1 and len(calls) == 1


@pytest.mark.parametrize('selected', [[], ['unknown'], ['boot-failure', 'boot-failure'], [None]])
def test_invalid_selection_rejected_before_transport(selected):
    with pytest.raises(ValueError):
        verifier()['verify']({}, transport=lambda *a, **k: pytest.fail('unexpected call'), case_ids=selected)
