import runpy
from pathlib import Path

from k3_support import semantic


def test_debug_unknown_canary_uses_production_prompt_and_never_accepts_reply():
    verifier = runpy.run_path(str(Path(__file__).resolve().parents[1] /
        'scripts/verify-research-canary.py'))['verify_debug_unknown']
    calls = []

    def transport(identity, **kwargs):
        calls.append(kwargs)
        assert kwargs['reasoning'] == 'medium'
        assert 'failure to reproduce on a test board is not proof' in kwargs['prompt']
        return {'ok': True, 'model': 'fixture', 'provider': 'fixture', 'manifest_digest': 'fixture',
            'result': {'decision_id': 'hermes-review-synthetic-debug', 'case_id': 'K3-SYNTHETIC',
                'expected_case_version': 3, 'intent': 'reply', 'confidence': .99, 'evidence_ids': [],
                'reply_draft': 'fixed', 'proposed_actions': [], 'facts': [], 'inferences': [], 'unknowns': []}}

    result = verifier({}, transport=transport)
    assert not result['ok'] and len(calls) == 1
    assert not result['live_messages_sent'] and not result['board_used']
    assert 'fixed' not in str(result)


def verify(transport):
    return runpy.run_path(str(Path(__file__).resolve().parents[1] /
                             'scripts/verify-research-canary.py'))['verify']({}, transport=transport)


def test_production_selector_uses_shared_prompt(monkeypatch):
    def transport(prompt, **kwargs):
        assert prompt == semantic.research_link_prompt({'query': 'fixture'})
        assert kwargs['reasoning'] == 'low'
        return {'document_urls': [], 'confidence': 0}
    monkeypatch.setattr(semantic, '_hermes_json', transport)
    assert semantic.hermes_research_link_selector({'query': 'fixture'})['confidence'] == 0


def test_canary_keeps_expectations_local_and_uses_low_effort():
    calls = []
    def transport(identity, **kwargs):
        assert kwargs['reasoning'] == 'low' and kwargs['timeout'] == 30
        assert 'expected' not in kwargs['prompt']
        calls.append(1)
        return {'ok': True, 'result': {'document_urls': ['https://example.com/fan'] if len(calls) == 1 else [],
                                     'confidence': .99 if len(calls) == 1 else 0},
                'model': 'fixture', 'provider': 'fixture', 'manifest_digest': 'fixture'}
    assert verify(transport)['ok'] and len(calls) == 2


def test_canary_stops_without_leaking_or_retrying():
    calls = []
    def transport(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('PRIVATE')
    result = verify(transport)
    assert not result['ok'] and len(calls) == 1 and 'PRIVATE' not in str(result)


def test_clarification_canary_requires_rejection_and_never_approves():
    from k3_support.clarification_context import REVIEW_FIELDS
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/verify-research-canary.py'))
    calls = []
    def transport(identity, **kwargs):
        calls.append(kwargs)
        assert kwargs['reasoning'] == 'medium'
        return {'ok': True, 'result': {**dict.fromkeys(REVIEW_FIELDS), 'decision': 'reject',
                                     'confidence': .99, 'reason_code': 'too_broad'},
                'model': 'fixture', 'provider': 'fixture', 'manifest_digest': 'fixture'}
    result = module['verify_clarification']({}, transport=transport)
    assert result['ok'] and len(calls) == 2 and not result['approval_created']
