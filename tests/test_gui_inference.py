import threading
import time
import uuid

import pytest
import json


def test_draft_view_only_contains_new_reply_text_and_marks_limits():
    from k3_support.gui_inference import _draft_previews
    rows = [{'channel': channel, 'action_type': action,
             'payload_json': json.dumps({'text': text, 'identity': 'private-owner', 'token': 'secret'})}
            for channel, action, text in [('telegram', 'owner_decision', 'not shown'),
                ('feishu_im', 'ack', 'not shown'), ('feishu_im', 'reply', 'x' * 4001),
                ('feishu_im', 'clarify', '<script>literal</script>')]]
    result = _draft_previews({'intentions': {'outbox': rows}})
    assert len(result['items']) == 2 and not result['sent']
    assert result['items'][0]['truncated'] and len(result['items'][0]['text']) == 4000
    assert result['items'][1]['text'] == '<script>literal</script>'
    assert 'secret' not in json.dumps(result) and 'private-owner' not in json.dumps(result)


@pytest.mark.parametrize('include_drafts', [False, True])
def test_gui_draft_opt_in_follows_actual_clarification_pipeline(conn, config, monkeypatch, include_drafts):
    from test_replay_model_pipeline import local_executor, request
    from test_workflow_replay import group_event
    from test_routing import route_value
    from test_clarification_context import positive_review
    from k3_support import semantic
    from k3_support.config import Config, validate_config
    from k3_support.gui_inference import infer
    local_executor(monkeypatch)
    value = request(config)
    value['event'] = group_event(1, '升级到新版后启动卡住了')
    value['assumptions']['observed_at'] = '2030-01-01T10:00:00+08:00'
    question = '现场复现使用的软件版本号是什么？'
    monkeypatch.setattr(semantic, 'hermes_message_router', lambda context, **kwargs:
        route_value('clarify', issue_type='bug', severity='P2', reason_codes=['missing_version'],
                    clarification_question=question, fallback_route='research'))
    monkeypatch.setattr(semantic, 'hermes_research_link_selector',
                        lambda context, **kwargs: {'document_urls': [], 'confidence': 0})
    monkeypatch.setattr(semantic, 'hermes_clarification_reviewer',
                        lambda context, **kwargs: positive_review(context))
    before = conn.serialize()
    result = infer(Config(validate_config(value['config']), config.path), value['event'],
                   assumptions=value['assumptions'], documents=[], review_clarification=True,
                   include_drafts=include_drafts)
    assert (question in json.dumps(result, ensure_ascii=False)) is include_drafts
    assert result['content_included'] is include_drafts
    assert not result['external_consumers']
    if include_drafts:
        assert len(result['drafts']['items']) == 1
        assert result['drafts']['items'][0]['kind'] == 'clarify'
        assert not result['drafts']['sent']
    assert conn.serialize() == before


@pytest.mark.parametrize('extra', [{'include_drafts': True},
    {'documents': [], 'include_drafts': 'yes'}])
def test_draft_content_requires_explicit_pipeline_selection(config, extra):
    tasks = PreviewTasks(config, lambda *args, **kwargs: pytest.fail('unexpected launch'))
    with pytest.raises(ValueError):
        tasks.start('session', {**payload(), **extra})
    assert not tasks.tasks


def test_gui_conversation_runs_same_snapshot_without_message_content_in_summary(conn, config, monkeypatch):
    from test_replay_model_pipeline import local_executor, request
    from test_routing import route_value
    from k3_support import semantic
    from k3_support.config import Config, validate_config
    from k3_support.gui_inference import infer
    local_executor(monkeypatch)
    value = request(config)
    monkeypatch.setattr(semantic, 'hermes_message_router',
                        lambda context, **kwargs: route_value('owner_decision'))
    before = conn.serialize()
    result = infer(Config(validate_config(value['config']), config.path), value['event'],
                   assumptions=value['assumptions'], documents=[], followups=['不是Pico，是EVB'])
    assert result['preview_kind'] == 'conversation'
    assert len(result['turns']) == 2
    assert all(turn['model_invoked'] is None and turn['model_callback_invoked']
               for turn in result['turns'])
    assert [call['turn'] for call in result['calls']] == [0, 1]
    assert not result['external_consumers']
    assert '不是Pico，是EVB' not in json.dumps(result, ensure_ascii=False)
    assert conn.serialize() == before


@pytest.mark.parametrize('followups', [[], ['x'] * 10, [None], ['x' * 2001]])
def test_gui_rejects_invalid_followups_before_launch(config, followups):
    tasks = PreviewTasks(config, lambda *args, **kwargs: pytest.fail('launched'))
    with pytest.raises(ValueError):
        tasks.start('session', {**payload(), 'documents': [], 'followups': followups})
    assert not tasks.tasks


@pytest.mark.parametrize('exit_status', ['captured', None, 1, 0])
def test_gui_debug_result_summary_uses_sealed_review(conn, config, monkeypatch, exit_status):
    from test_replay_debug_snapshot import request, local_executor, wait_decision
    from k3_support import semantic
    from k3_support.config import Config, validate_config
    from k3_support.gui_inference import infer
    value = request(conn, config)
    debug = {'job_id': value['job_id'], 'transcript': value['transcript']}
    if exit_status != 'captured':
        from test_review import result_text
        case = conn.execute('SELECT case_id FROM jobs').fetchone()[0]
        conn.execute("UPDATE jobs SET state='queued',available_at='2020-01-01T00:00:00+00:00'")
        debug['execution'] = {'report': result_text(case), 'exit_status': exit_status}
    local_executor(monkeypatch)
    monkeypatch.setattr(semantic, '_hermes_json', lambda prompt, **kwargs:
                        json.loads(wait_decision({'prompt': prompt})))
    before = conn.serialize()
    result = infer(Config(validate_config(value['config']), config.path), {},
                   debug=debug)
    assert result['preview_kind'] == ('captured_debug_review' if exit_status == 'captured' else 'simulated_debug_execution')
    assert result['review_ok'] is (exit_status in ('captured', 0))
    assert result['model_callback_invoked'] is (exit_status in ('captured', 0))
    assert not result['content_included'] and not result['external_consumers']
    assert 'Caller failure not reproduced' not in str(result)
    assert conn.serialize() == before


@pytest.mark.parametrize('extra', [{'event': {'content': 'mixed'}}, {'documents': []},
    {'review_clarification': False}, {'assumptions': {'mode': 'auto'}}])
def test_debug_preview_rejects_mixed_scope_before_launch(config, extra):
    value = payload()
    value.update(event={}, debug={'job_id': 'job', 'transcript': []})
    value.update(extra)
    tasks = PreviewTasks(config, lambda *a, **k: pytest.fail('launched'))
    with pytest.raises(ValueError, match='不能混入'):
        tasks.start('session', value)

from k3_support.gui_inference import PreviewTasks


def payload():
    return {'request_id': str(uuid.uuid4()), 'event': {'payload': {'content': 'question'}},
            'confirm_model_call': True}


def finished(tasks, session, identifier):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = tasks.status(session, identifier)
        if result['state'] != 'running':
            return result
        time.sleep(.001)
    pytest.fail('preview did not finish')


def test_preview_nonblocking_single_flight_private_idempotent(config):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def runner(cfg, event):
        calls.append(event)
        entered.set()
        assert release.wait(2)
        return {'scope': 'routing', 'external_consumers': False}

    tasks = PreviewTasks(config, runner)
    value = payload()
    try:
        assert tasks.start('session1', value)['state'] == 'running'
        assert entered.wait(1)
        assert tasks.start('session1', value)['state'] == 'running'
        assert len(calls) == 1
        with pytest.raises(ValueError, match='进行中'):
            tasks.start('session2', payload())
        with pytest.raises(ValueError, match='未找到'):
            tasks.status('session2', value['request_id'])
    finally:
        release.set()
    result = finished(tasks, 'session1', value['request_id'])
    assert result['state'] == 'completed'
    assert 'digest' not in result
    second = payload()
    tasks.start('session1', second)
    finished(tasks, 'session1', second['request_id'])
    assert tasks.start('session1', value) == result
    assert len(calls) == 2
    value['event'] = {}
    with pytest.raises(ValueError, match='不能更改'):
        tasks.start('session1', value)


def test_preview_failure_never_leaks_error_or_retries(config):
    calls = []

    def runner(*args):
        calls.append(1)
        raise RuntimeError('secret-provider-response')

    tasks = PreviewTasks(config, runner)
    value = payload()
    tasks.start('session', value)
    result = finished(tasks, 'session', value['request_id'])
    assert result['state'] == 'failed'
    assert 'secret' not in str(result)
    assert tasks.start('session', value) == result
    assert calls == [1]


def test_task_passes_bound_assumptions_to_runner(config):
    seen = []

    def runner(cfg, event, *, assumptions):
        seen.append(assumptions)
        return {'assumptions': assumptions}

    tasks = PreviewTasks(config, runner)
    value = payload()
    value['assumptions'] = {'mode': 'observe'}
    tasks.start('session', value)
    result = finished(tasks, 'session', value['request_id'])
    assert result['result']['assumptions'] == {'mode': 'observe'}
    value['assumptions'] = {'mode': 'auto'}
    with pytest.raises(ValueError, match='不能更改'):
        tasks.start('session', value)
    assert seen == [{'mode': 'observe'}]


def test_gui_two_stage_uses_sealed_driver_and_returns_summary_only(conn, config, monkeypatch):
    from test_replay_model_pipeline import local_executor, request
    from test_routing import route_value
    from k3_support import semantic
    from k3_support.config import Config, validate_config
    from k3_support.gui_inference import infer

    local_executor(monkeypatch)
    value = request(config)
    cfg = Config(validate_config(value['config']), config.path)
    monkeypatch.setattr(semantic, 'hermes_message_router', lambda *args, **kwargs: route_value('research'))
    monkeypatch.setattr(semantic, 'hermes_research_link_selector', lambda *args, **kwargs: {'document_urls': ['https://example.com/fan'], 'confidence': .99})
    before = conn.serialize()
    result = infer(cfg, value['event'], documents=value['documents'], assumptions=value['assumptions'])
    assert result['preview_kind'] == 'routing_and_document_selection'
    assert result['steps'][1]['state'] == 'needs_owner_review'
    assert len(result['calls']) == 2
    assert result['intention_counts']['outbox'] >= 1
    assert not result['content_included'] and not result['external_consumers']
    assert 'https://example.com/fan' not in str(result)
    assert conn.serialize() == before


def test_invalid_documents_rejected_before_task_start(config):
    tasks = PreviewTasks(config, lambda *args, **kwargs: pytest.fail('started'))
    value = payload()
    value['documents'] = [{'title': 'bad', 'url': 'file:///private', 'content': ''}]
    with pytest.raises(ValueError):
        tasks.start('session', value)
    assert not tasks.tasks


@pytest.mark.parametrize('flag', ['true', 1, None, True])
def test_review_requires_boolean_and_document_scope(config, flag):
    tasks = PreviewTasks(config, lambda *args, **kwargs: pytest.fail('started'))
    value = payload()
    value['review_clarification'] = flag
    with pytest.raises(ValueError, match='追问审核'):
        tasks.start('session', value)


def test_gui_explicit_review_runs_third_stage_without_delivery(conn, config, monkeypatch):
    from test_replay_model_pipeline import local_executor, request
    from test_routing import route_value
    from test_clarification_context import positive_review
    from k3_support import semantic
    from k3_support.config import Config, validate_config
    from k3_support.gui_inference import infer

    local_executor(monkeypatch)
    value = request(config)
    value['event']['payload']['content'] = '升级到新版后启动卡住了'
    cfg = Config(validate_config(value['config']), config.path)
    monkeypatch.setattr(semantic, 'hermes_message_router', lambda *a, **k: route_value(
        'clarify', issue_type='bug', severity='P2', reason_codes=['missing_version'],
        clarification_question='现场复现使用的软件版本号是什么？', fallback_route='research'))
    monkeypatch.setattr(semantic, 'hermes_research_link_selector', lambda *a, **k:
        {'document_urls': [], 'confidence': 0})
    monkeypatch.setattr(semantic, 'hermes_clarification_reviewer', lambda value, **k: positive_review(value))
    before = conn.serialize()
    result = infer(cfg, value['event'], documents=value['documents'],
                   assumptions=value['assumptions'], review_clarification=True)
    assert result['preview_kind'] == 'routing_research_clarification'
    assert [call['stage'] for call in result['calls']] == [
        'routing', 'research_selection', 'clarification_review']
    assert not result['external_consumers']
    assert conn.serialize() == before


def test_submit_distinguishes_rejection_from_existing_request(config):
    tasks = PreviewTasks(config, lambda *args, **kwargs: {})
    invalid = payload()
    invalid['assumptions'] = {'observed_at': 'not-a-time'}
    result = tasks.submit('session', invalid)
    assert result['state'] == 'rejected' and result['execution_state'] == 'not_started'
    assert not tasks.tasks
    value = payload()
    tasks.submit('session', value)
    finished(tasks, 'session', value['request_id'])
    value['event'] = {'changed': True}
    with pytest.raises(ValueError, match='不能更改'):
        tasks.submit('session', value)


def test_gui_infer_reuses_frozen_production_route_without_consumers(conn, config, monkeypatch):
    from test_replay_inference import local_executor
    from test_routing import active_config, route_value
    from test_workflow_replay import event
    from k3_support import semantic
    from k3_support.gui_inference import infer

    local_executor(monkeypatch)
    calls = []

    def router(value, *, timeout):
        calls.append((value, timeout))
        return route_value('owner_decision', requires_owner_judgment=True,
                           reason_codes=['requires_commitment'])

    monkeypatch.setattr(semantic, 'hermes_message_router', router)
    before = conn.serialize()
    result = infer(active_config(config), event())
    assert len(calls) == 1 and calls[0][1] == 45
    assert result['steps'][0]['route'] == 'owner_decision'
    assert not result['external_consumers'] and not result['content_included']
    assert result['model_callback_invoked'] is True
    assert result['model_invoked'] is None
    assert conn.serialize() == before


@pytest.mark.parametrize('change', [{'confirm_model_call': False}, {'event': []}, {'request_id': 'bad'}, {'proposal': {}}])
def test_preview_rejects_unconfirmed_or_extra_inputs(config, change):
    tasks = PreviewTasks(config, lambda *args: pytest.fail('must not run'))
    value = {**payload(), **change}
    with pytest.raises(ValueError):
        tasks.start('session', value)
    assert not tasks.tasks
