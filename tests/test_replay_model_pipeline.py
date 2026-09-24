import json
import tempfile
from unittest.mock import patch

import pytest
from test_routing import active_config, route_value
from test_workflow_replay import group_event

from k3_support import replay_history, replay_model_pipeline as pipeline


def request(config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    return {'config': cfg.raw, 'event': group_event(1, 'Pico风扇怎么调节'),
            'documents': [{'title': 'Pico风扇', 'url': 'https://example.com/fan', 'content': '操作说明'}],
            'assumptions': {'relationship': 'peer', 'function_role': 'engineering'}}


def local_executor(monkeypatch):
    def process(**kwargs):
        assert 'replay_model_pipeline import execute' in kwargs['argv'][-1]
        original_config = pipeline.Config
        with tempfile.TemporaryDirectory(prefix='codex-replay-test-') as scratch:
            def config(raw, path):
                raw['paths']['data_dir'] = scratch
                return original_config(raw, path)
            with patch.object(pipeline, 'Config', config):
                return json.dumps(pipeline.execute(json.loads(kwargs['stdin']),
                                                  f'/proc/self/fd/{kwargs["pass_fds"][0]}'))
    monkeypatch.setattr(replay_history, 'run_process', process)


def test_sealed_pipeline_calls_each_stage_once_and_preserves_source(conn, config, monkeypatch):
    local_executor(monkeypatch)
    before = conn.serialize()
    calls = []

    def router(value):
        calls.append('routing')
        return route_value('research')

    def selector(value):
        calls.append('research_selection')
        assert value['documents'][0]['url'] == 'https://example.com/fan'
        return {'document_urls': ['https://example.com/fan'], 'confidence': .99}

    result = pipeline.run(config.database_path, request(config), router=router, selector=selector)
    assert calls == ['routing', 'research_selection']
    assert [row['stage'] for row in result['calls']] == calls
    assert result['result']['research']['completion']['reason'] == 'document_route_not_evaluated'
    assert result['assumptions']['observed_at']
    assert conn.serialize() == before


def test_pipeline_rejects_expected_output_before_source(config, monkeypatch):
    value = request(config)
    value['answers'] = []
    monkeypatch.setattr(pipeline, '_snapshot_data', lambda _: pytest.fail('source accessed'))
    with pytest.raises(ValueError):
        pipeline.run(config.database_path, value, router=lambda _: None, selector=lambda _: None)


def test_pipeline_changed_observation_stops_before_next_model(config, monkeypatch):
    rounds = []
    monkeypatch.setattr(pipeline, '_snapshot_data', lambda _: b'fixed')

    def execute(*args, **kwargs):
        rounds.append(1)
        return {'requests': [{'stage': 'routing', 'input': {'query': str(len(rounds))}}]}

    monkeypatch.setattr(pipeline, '_run_snapshot_data', execute)
    calls = []
    with pytest.raises(ValueError, match='observations changed'):
        pipeline.run(config.database_path, request(config), router=lambda _: (calls.append(1) or route_value('research')),
                     selector=lambda _: pytest.fail('selector called'))
    assert calls == [1]


def test_pipeline_rejects_untrusted_reviewer_before_source(config, monkeypatch):
    monkeypatch.setattr(pipeline, '_snapshot_data', lambda _: pytest.fail('source accessed'))
    with pytest.raises(ValueError, match='trusted callback'):
        pipeline.run(config.database_path, request(config), router=lambda _: None,
                     selector=lambda _: None, clarification_reviewer='approve')


def test_conversation_preserves_prior_turn_and_source(conn, config, monkeypatch):
    local_executor(monkeypatch)
    value = request(config)
    del value['event']
    value['events'] = [group_event(1, 'Pico启动失败'),
                       group_event(2, '不是Pico，是EVB', parent='om_group_1')]
    before = conn.serialize()
    observations = []

    def router(context):
        observations.append(json.dumps(context, ensure_ascii=False))
        return route_value('owner_decision')

    result = pipeline.run(config.database_path, value, router=router,
                          selector=lambda _: pytest.fail('unexpected research'))
    turns = result['result']['turns']
    assert len(turns) == 2
    assert turns[0]['inbound']['result']['case_id'] == turns[1]['inbound']['result']['case_id']
    assert len(observations) == 2
    assert 'Pico启动失败' in observations[1] and '不是Pico，是EVB' in observations[1]
    assert [call['turn'] for call in result['calls']] == [0, 1]
    assert conn.serialize() == before


def test_conversation_owner_reply_suppresses_later_model_calls(conn, config, monkeypatch):
    local_executor(monkeypatch)
    value = request(config)
    value.pop('event')
    owner = group_event(2, '我来排查，你先不用操作', parent='om_group_1')
    owner['sender_id'] = 'ou_owner'
    value['events'] = [group_event(1, '启动失败'), owner,
                       group_event(3, '补充日志', parent='om_group_1')]
    before = conn.serialize()
    calls = []

    def router(context):
        calls.append(context)
        assert len(calls) == 1, 'owner reply must fence subsequent model routing'
        return route_value('owner_decision')

    result = pipeline.run(config.database_path, value, router=router,
                          selector=lambda _: pytest.fail('unexpected retrieval'))
    assert len(result['result']['turns']) == 3
    assert len(calls) == 1
    for turn in result['result']['turns'][1:]:
        assert not turn['intentions']['jobs']
        assert not turn['intentions']['outbox']
    assert conn.serialize() == before


@pytest.mark.parametrize('events', [[], [{}] * 11, 'messages', [None]])
def test_invalid_conversation_rejected_before_snapshot(config, monkeypatch, events):
    value = request(config)
    value.pop('event')
    value['events'] = events
    monkeypatch.setattr(pipeline, '_snapshot_data', lambda _: pytest.fail('source read'))
    with pytest.raises(ValueError):
        pipeline.run(config.database_path, value, router=lambda _: None, selector=lambda _: None)


@pytest.mark.parametrize('approve', [False, True])
def test_pipeline_research_clarification_review_is_optional_and_once(conn, config, monkeypatch, approve):
    local_executor(monkeypatch)
    value = request(config)
    value['event'] = group_event(1, '升级到新版后启动卡住了')
    value['assumptions']['observed_at'] = '2030-01-01T10:00:00+08:00'
    before = conn.serialize()
    seen = []

    def reviewer(context):
        from test_clarification_context import positive_review
        seen.append(context)
        return positive_review(context) if approve else {'decision': 'reject'}

    result = pipeline.run(config.database_path, value,
        router=lambda _: route_value('clarify', issue_type='bug', severity='P2',
            reason_codes=['missing_version'], clarification_question='现场复现使用的软件版本号是什么？',
            fallback_route='research'),
        selector=lambda _: {'document_urls': [], 'confidence': 0},
        clarification_reviewer=reviewer)
    assert len(seen) == 1
    assert [row['stage'] for row in result['calls']] == [
        'routing', 'research_selection', 'clarification_review']
    assert any(row['action_type'] == 'clarify' for row in result['result']['intentions']['outbox']) is approve
    assert pipeline._replay_id_factory.get() is None
    assert conn.serialize() == before
