import pytest
from test_routing import active_config, route_value
from test_workflow_replay import group_event

from k3_support.replay_scenario import execute_scenario


def scenario(config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    return {'config': cfg.raw, 'steps': [
        {'event': group_event(1, 'Pico启动失败'), 'proposal': route_value('research')},
        {'case_step': 0, 'action': 'claim'},
        {'event': group_event(2, '补充UFS失败', parent='om_group_1'), 'proposal': route_value('research')},
    ]}


def test_scenario_can_observe_auto60_expiry_without_global_time_travel(config):
    request = scenario(config)
    request['steps'] = [{'global_mode':'auto_60'}, {'mode_elapsed_minutes':59}, {'mode_elapsed_minutes':60}]
    result = execute_scenario(request)
    assert [item['global_control']['mode'] for item in result['steps']] == ['auto_60', 'auto_60', 'collaborate']
    assert result['steps'][-1]['global_control']['clock_scope'] == 'global_mode_only_not_worker_leases'


@pytest.mark.parametrize('minutes', [True, -1, 1441, '60'])
def test_scenario_rejects_invalid_mode_elapsed(config, minutes):
    request = scenario(config)
    request['steps'] = [{'mode_elapsed_minutes':minutes}]
    with pytest.raises(ValueError):
        execute_scenario(request)


def test_scenario_runs_inbound_and_owner_control(config):
    result = execute_scenario(scenario(config))
    assert result['steps'][0]['result']['case_id']
    assert result['steps'][2]['result']['ignored']
    assert result['steps'][2]['intentions'] == {'jobs': [], 'outbox': []}
    assert result['model_invoked'] is False


def test_expected_answer_not_accepted(config):
    request = scenario(config)
    request['steps'][0]['expected_answer'] = 'must not enter inference'
    with pytest.raises(ValueError, match='unsupported'):
        execute_scenario(request)


def test_forward_control_reference_rejected(config):
    request = scenario(config)
    request['steps'][1]['case_step'] = 2
    with pytest.raises(ValueError, match='communication'):
        execute_scenario(request)


@pytest.mark.parametrize('relationship', ['supervisor', 'dotted_supervisor'])
def test_supervisor_is_not_sent_automatic_clarification(config, relationship):
    request = scenario(config)
    request['steps'] = request['steps'][:1]
    request['profiles'] = [{'requester_id': request['steps'][0]['event']['sender_id'],
                            'relationship': relationship, 'function_role': 'management'}]
    request['steps'][0]['proposal'] = route_value(
        'clarify', clarification_question='请提供全部日志？', reason_codes=['missing_logs'],
        fallback_route='research')
    result = execute_scenario(request)
    route = result['steps'][0]['result']['route']
    assert route['route'] != 'clarify'
    assert route['clarification_question'] is None
    assert result['profile_scope'] == 'scenario_assumptions_not_verified_directory'


@pytest.mark.parametrize('role', ['project_manager', 'product_manager'])
def test_cross_function_commitment_escalates_to_owner(config, role):
    request = scenario(config)
    request['steps'] = request['steps'][:1]
    incoming = request['steps'][0]['event']
    incoming['payload']['content'] = '这个UFS问题什么时候能交付？'
    request['profiles'] = [{'requester_id': incoming['sender_id'],
                            'relationship': 'cross_function', 'function_role': role}]
    request['steps'][0]['proposal'] = route_value('research', issue_type='request')
    result = execute_scenario(request)
    assert result['steps'][0]['result']['route']['route'] == 'owner_decision'


def test_duplicate_assumed_identity_is_rejected(config):
    request = scenario(config)
    profile = {'requester_id': 'ou_peer', 'relationship': 'peer', 'function_role': 'qa'}
    request['profiles'] = [profile, profile]
    with pytest.raises(ValueError, match='duplicate'):
        execute_scenario(request)


@pytest.mark.parametrize('status', ['candidate', 'approved', 'retired'])
def test_real_knowledge_retrieval_respects_fixture_status(config, status):
    request = scenario(config)
    request['steps'] = request['steps'][:1]
    incoming = request['steps'][0]['event']
    incoming['payload']['content'] = 'K3 怎么进入 fastboot'
    request['profiles'] = [{'requester_id': incoming['sender_id'],
                            'relationship': 'peer', 'function_role': 'engineering'}]
    request['knowledge_fixtures'] = [{'title': 'K3 Fastboot', 'questions': ['K3 怎么进入 fastboot'],
        'answer_markdown': '参考操作文档：https://example.com/fastboot',
        'disclosure_class': 'public', 'status': status}]
    request['steps'][0]['proposal'] = route_value('direct_answer', issue_type='faq',
        reason_codes=['approved_knowledge_match'], repository_hints=[])
    without_selection = execute_scenario(request)
    assert without_selection['steps'][0]['result']['route']['route'] != 'direct_answer'
    request['steps'][0]['knowledge_selection'] = {'fixture_index': 0, 'confidence': .99}
    result = execute_scenario(request)
    route = result['steps'][0]['result']['route']['route']
    assert (route == 'direct_answer') is (status == 'approved')
    assert result['knowledge_scope'] == 'synthetic_status_assumptions_not_reviewed_evidence'


def test_scenario_includes_retrieval_completion(config):
    from k3_support.replay_cli import summary

    request = scenario(config)
    request['steps'] = request['steps'][:1] + [{
        'research_for_step': 0,
        'documents': [{'title': 'Pico参考', 'url': 'https://example.com/pico', 'content': '测试文档'}],
        'selection': {'document_urls': ['https://example.com/pico'], 'confidence': .99},
    }]
    result = execute_scenario(request)
    assert result['steps'][1]['research']['retrieval']['followup_eligible']
    assert result['steps'][1]['research']['completion'] is not None
    compact = summary(result)
    assert compact['steps'][1]['kind'] == 'research'
    assert 'https://example.com/pico' not in str(compact)


@pytest.mark.parametrize('quote,allows', [('升级到新版后启动卡住了', True), ('不存在的现场信息', False)])
def test_contextual_question_review_after_research(config, quote, allows):
    from test_clarification_context import positive_review

    request = scenario(config)
    incoming = request['steps'][0]['event']
    incoming['payload']['content'] = '升级到新版后启动卡住了'
    template = positive_review({'messages': [{'event_pk': 'unused', 'content': quote}],
                                'available_sources_and_checks': [{'ref': 'unused'}]})
    template['source_quote'].pop('event_pk')
    template.pop('research_refs')
    template.update(source_message_index=0, research_ref_indexes=[0])
    request['profiles'] = [{'requester_id': incoming['sender_id'], 'relationship': 'peer', 'function_role': 'qa'}]
    request['steps'] = [{'event': incoming, 'proposal': route_value('clarify', issue_type='bug', severity='P2',
        reason_codes=['missing_version'], clarification_question='现场复现使用的软件版本号是什么？', fallback_route='research')},
        {'research_for_step': 0, 'documents': [{'title': 'Release notes', 'url': 'https://example.com/release',
          'content': 'A matching software revision is needed.'}], 'selection': None, 'clarification_review': template}]
    result = execute_scenario(request)
    assert result['steps'][0]['result']['route']['route'] == 'research'
    assert (result['steps'][1]['research']['completion']['state'] == 'clarification_queued') is allows


@pytest.mark.parametrize('mode', ['observe', 'collaborate', 'auto_60', 'auto', 'paused', 'stopped'])
def test_scenario_real_global_mode_callbacks(config, mode):
    request = scenario(config)
    request['steps'] = [{'global_mode': mode}]
    result = execute_scenario(request)
    assert result['steps'][0]['global_control']['mode'] == mode


@pytest.mark.parametrize('mode', ['paused', 'stopped'])
def test_disabled_mode_followup_creates_no_intentions(config, mode):
    request = scenario(config)
    request['steps'] = [{'global_mode': mode}, request['steps'][0]]
    result = execute_scenario(request)
    assert result['steps'][1]['intentions'] == {'outbox': [], 'jobs': []}


def test_resume_after_pause_processes_same_event_once(config):
    request = scenario(config)
    incoming = request['steps'][0]
    request['steps'] = [{'global_mode': 'paused'}, incoming,
        {'global_mode': 'auto_60'}, {'resume_step': 1, 'proposal': route_value('research')},
        {'resume_step': 1, 'proposal': route_value('research')}]
    result = execute_scenario(request)
    assert result['steps'][1]['result']['blocked_by_mode'] == 'paused'
    assert result['steps'][3]['result']['case_id']
    assert result['steps'][3]['event_pk'] == result['steps'][1]['event_pk']
    assert result['steps'][4]['intentions'] == {'outbox': [], 'jobs': []}


def test_backlog_replay_discards_superseded_question_and_processes_latest(config):
    request = scenario(config)
    request['steps'] = [
        {'global_mode': 'paused'},
        {'event': group_event(1, 'Pico启动失败'), 'proposal': route_value('research')},
        {'event': group_event(2, '纠正：不是Pico，是EVB', parent='om_group_1'), 'proposal': route_value('research')},
        {'global_mode': 'auto_60'},
        {'resume_step': 1, 'proposal': route_value('research')},
        {'resume_step': 2, 'proposal': route_value('research')},
        {'resume_step': 1, 'proposal': route_value('research')},
        {'resume_step': 2, 'proposal': route_value('research')},
    ]
    result = execute_scenario(request)
    assert result['steps'][4]['result']['superseded']
    assert result['steps'][4]['intentions'] == {'outbox': [], 'jobs': []}
    assert result['steps'][5]['result']['case_id']
    assert result['steps'][5]['intentions']['jobs']
    assert result['steps'][6]['intentions'] == {'outbox': [], 'jobs': []}
    assert result['steps'][7]['intentions'] == {'outbox': [], 'jobs': []}


@pytest.mark.parametrize('route,issue,severity,reason', [
    ('ignore', 'investigation', 'P3', 'non_work_noise'),
    ('urgent_notify', 'incident', 'P0', 'severe_outage'),
    ('codex_debug', 'bug', 'P2', 'technical_investigation'),
])
def test_remaining_route_intentions_without_external_execution(config, route, issue, severity, reason):
    request = scenario(config)
    request['steps'] = request['steps'][:1]
    request['steps'][0]['proposal'] = route_value(route, issue_type=issue, severity=severity,
                                               reason_codes=[reason])
    if route == 'codex_debug':
        request['steps'].append({'research_for_step': 0, 'documents': [], 'selection': None})
    result = execute_scenario(request)
    item = result['steps'][0]
    assert item['result']['route']['route'] == route
    if route == 'ignore':
        assert item['intentions'] == {'outbox': [], 'jobs': []}
    elif route == 'codex_debug':
        assert any(job['job_type'] == 'retrieve' and job['state'] == 'queued'
                   for job in item['intentions']['jobs'])
        # Debug follows the prerequisite lookup; no coding process is launched.
        completion = result['steps'][1]['research']['completion']
        assert completion['job_id']
        assert completion['parent_job_id'] == item['intentions']['jobs'][0]['job_id']
        assert result['steps'][1]['research']['child_job']['state'] == 'queued'
    else:
        assert any(row['channel'] == 'telegram' for row in item['intentions']['outbox'])
        assert not any(row['action_type'] in {'reply', 'clarify'} for row in item['intentions']['outbox'])
    assert not result['model_invoked']


def test_global_auto_does_not_override_case_takeover(config):
    request = scenario(config)
    request['steps'] = request['steps'][:2] + [
        {'global_mode': 'paused'}, {'global_mode': 'auto'},
        {'event': group_event(2, '补充UFS失败', parent='om_group_1'), 'proposal': route_value('research')},
    ]
    result = execute_scenario(request)
    assert result['steps'][3]['global_control']['mode'] == 'auto'
    assert result['steps'][4]['result']['ignored']
    assert result['steps'][4]['intentions'] == {'outbox': [], 'jobs': []}


def test_explicit_case_delegate_allows_new_followup_after_takeover(config):
    request = scenario(config)
    request['steps'] = request['steps'][:2] + [
        {'case_step': 0, 'action': 'delegate'},
        {'event': group_event(2, '补充UFS失败', parent='om_group_1'), 'proposal': route_value('research')},
    ]
    result = execute_scenario(request)
    assert result['steps'][3]['result']['case_id'] == result['steps'][0]['result']['case_id']
    assert not result['steps'][3]['result'].get('ignored', False)


@pytest.mark.parametrize('codex_enabled', [False, True])
def test_retrieval_timeout_respects_codex_feature(config, codex_enabled):
    request = scenario(config)
    request['config']['features']['codex'] = codex_enabled
    request['steps'] = request['steps'][:1] + [{
        'research_for_step': 0, 'documents': [], 'selection': None, 'fail_transport': True}]
    result = execute_scenario(request)
    research = result['steps'][1]['research']
    assert research['retrieval']['state'] == 'failed'
    assert bool(research['completion'] and research['completion'].get('job_id')) is codex_enabled
