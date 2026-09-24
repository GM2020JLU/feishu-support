import runpy
from pathlib import Path

from test_routing import route_value


def verifier():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/verify-research-pipeline.py'))['verify']


def test_pipeline_canary_calls_two_shared_prompts_and_preserves_gate():
    calls = []

    def transport(identity, *, prompt, timeout, reasoning):
        calls.append((prompt, timeout, reasoning))
        output = route_value('research') if len(calls) == 1 else {'document_urls': ['https://example.com/fan'], 'confidence': .99}
        return {'ok': True, 'result': output, 'model': 'synthetic', 'provider': 'synthetic', 'manifest_digest': 'fixture'}

    result = verifier()({}, transport=transport)
    assert result['ok'] and result['source_unchanged']
    assert [call[2] for call in calls] == ['medium', 'low']
    assert all(call[1] == 45 for call in calls)
    assert result['completion_reason'] == 'document_route_not_evaluated'
    assert result['owner_link_intent']
    assert not result['live_messages_sent'] and not result['human_gold_reviewed']


def test_bad_receipt_does_not_retry_or_advance():
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return {'ok': False}

    # Production routing may fail closed without raising; either way there is
    # no second provider request and no successful pipeline acceptance.
    try:
        result = verifier()({}, transport=transport)
        assert not result['ok']
    except ValueError:
        pass
    assert calls == [1]
