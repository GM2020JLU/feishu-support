"""Bounded fixture-driven workflow scenarios in a fresh isolated database.

Proposals are injected test inputs, never model-quality evidence. No expected
answer fields are accepted. Historical knowledge/model RPC integration is pending.
"""
from __future__ import annotations

import copy
import sqlite3
import sysconfig
from pathlib import Path

from .config import Config, validate_config
from .db import migrate
from .ids import digest
from .knowledge import DISCLOSURE_LEVELS, create_candidate, review
from .replay_modes import observe_mode_elapsed, switch_mode
from .replay_research import finish_research, fixture_reviewer
from .replay_sandbox import run_sandbox
from .routing import (
    FUNCTION_ROLES,
    RELATIONSHIPS,
    set_requester_profile,
    validate_route_output,
)
from .runtime_control import MODES
from .workflow_replay import replay_communication, replay_inbound, resume_inbound


def execute_scenario(request: dict) -> dict:
    if (not isinstance(request, dict) or not {'config', 'steps'} <= set(request)
            or set(request) - {'config', 'steps', 'profiles', 'knowledge_fixtures'}):
        raise ValueError('scenario requires config, steps and optional profiles/knowledge_fixtures')
    knowledge = request.get('knowledge_fixtures', [])
    if not isinstance(knowledge, list) or len(knowledge) > 30:
        raise ValueError('at most 30 knowledge fixtures allowed')
    for entry in knowledge:
        if not isinstance(entry, dict) or set(entry) != {'title', 'questions', 'answer_markdown', 'disclosure_class', 'status'}:
            raise ValueError('invalid knowledge fixture fields')
        for field, limit in [('title', 300), ('answer_markdown', 12000)]:
            if not isinstance(entry[field], str) or not 1 <= len(entry[field].strip()) <= limit:
                raise ValueError('invalid knowledge fixture text')
        if (not isinstance(entry['questions'], list) or not 1 <= len(entry['questions']) <= 20
                or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 2000 for q in entry['questions'])):
            raise ValueError('invalid knowledge fixture questions')
        if (entry['status'] not in ['candidate', 'approved', 'retired']
                or not isinstance(entry['disclosure_class'], str)
                or entry['disclosure_class'] not in DISCLOSURE_LEVELS):
            raise ValueError('invalid assumed knowledge status or visibility')
    profiles = request.get('profiles', [])
    if not isinstance(profiles, list) or len(profiles) > 30:
        raise ValueError('scenario allows at most 30 assumed profiles')
    seen = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != {'requester_id', 'relationship', 'function_role'}:
            raise ValueError('invalid assumed profile fields')
        requester = profile['requester_id']
        if not isinstance(requester, str) or not 1 <= len(requester) <= 256 or requester in seen:
            raise ValueError('invalid or duplicate requester')
        if (not isinstance(profile['relationship'], str) or not isinstance(profile['function_role'], str)
                or profile['relationship'] not in RELATIONSHIPS or profile['function_role'] not in FUNCTION_ROLES):
            raise ValueError('invalid assumed role')
        seen.add(requester)
    steps = request['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise ValueError('scenario requires 1 to 30 steps')
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise TypeError('invalid scenario step')
        selection = step.get('knowledge_selection')
        if 'knowledge_selection' in step and (
                    not isinstance(selection, dict) or set(selection) != {'fixture_index', 'confidence'}
                    or type(selection['fixture_index']) is not int
                    or not 0 <= selection['fixture_index'] < len(knowledge)
                    or type(selection['confidence']) not in (int, float)
                    or not 0 <= selection['confidence'] <= 1):
            raise ValueError('invalid explicit knowledge selection fixture')
        if set(step) - {'knowledge_selection'} == {'event', 'proposal'}:
            validate_route_output(step['proposal'])
        elif set(step) - {'knowledge_selection'} == {'resume_step', 'proposal'}:
            if type(step['resume_step']) is not int or not 0 <= step['resume_step'] < index:
                raise ValueError('invalid resume reference')
            validate_route_output(step['proposal'])
        elif set(step) == {'global_mode'}:
            if not isinstance(step['global_mode'], str) or step['global_mode'] not in MODES:
                raise ValueError('invalid global mode')
        elif set(step) == {'mode_elapsed_minutes'}:
            if type(step['mode_elapsed_minutes']) is not int or not 0 <= step['mode_elapsed_minutes'] <= 1440:
                raise ValueError('invalid mode elapsed minutes')
        elif set(step) == {'case_step', 'action'}:
            if (type(step['case_step']) is not int or not 0 <= step['case_step'] < index
                    or step['action'] not in {'claim', 'suggest_only', 'delegate', 'details'}):
                raise ValueError('invalid communication step')
        elif ({'research_for_step', 'documents', 'selection'} <= set(step)
              and not set(step) - {'research_for_step', 'documents', 'selection', 'clarification_review', 'fail_transport'}):
            if type(step['research_for_step']) is not int or not 0 <= step['research_for_step'] < index:
                raise ValueError('invalid research reference')
            if 'fail_transport' in step and type(step['fail_transport']) is not bool:
                raise ValueError('fail_transport must be explicit boolean')
        else:
            raise ValueError('unsupported scenario step fields')
    raw = copy.deepcopy(request['config'])
    raw['paths'] = {'database': '/tmp/replay.db', 'data_dir': '/tmp/replay'}
    config = Config(validate_config(raw), Path('/tmp/replay.yaml'))
    if any('action' in step for step in steps) and not config.raw['identity']['telegram_control_user_id']:
        raise ValueError('communication scenarios require a simulated owner identity')
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        migrate(conn)
        knowledge_ids = []
        for entry in knowledge:
            knowledge_id = create_candidate(
                conn, title=entry['title'], questions=entry['questions'],
                answer_markdown=entry['answer_markdown'], project='K3', module='bootloader',
                software_version=None, disclosure_class=entry['disclosure_class'],
                confidence=.97, source_authority=.97, canonical_case_id=None,
                source_digest=digest({'synthetic_replay_fixture': entry}))
            review(conn, knowledge_id=knowledge_id, reviewer_id='synthetic-replay-not-human-review',
                   decision=entry['status'])
            knowledge_ids.append(knowledge_id)
        # This is fixture preparation in an isolated replay database, not a
        # query-time rebuild or permission to publish synthetic knowledge.
        from .knowledge_corpus import build
        if not build(conn)['built']:
            raise ValueError('synthetic replay corpus changed during preparation')
        for profile in profiles:
            set_requester_profile(conn, **profile, source='operator',
                                  evidence={'scenario_assumption': True})
        reports = []
        for index, step in enumerate(steps):
            selection = step.get('knowledge_selection')
            selector = (None if selection is None else
                lambda query, catalog, choice=selection: {
                    'knowledge_id': knowledge_ids[choice['fixture_index']],
                    'confidence': choice['confidence']})
            if 'global_mode' in step:
                reports.append({'global_control': switch_mode(conn, config,
                    mode=step['global_mode'], step_id=index)})
            elif 'mode_elapsed_minutes' in step:
                reports.append({'global_control': observe_mode_elapsed(conn, config, step['mode_elapsed_minutes'])})
            elif 'event' in step:
                proposal = copy.deepcopy(step['proposal'])
                reports.append(replay_inbound(conn, config, step['event'],
                                              semantic_selector=selector,
                                              message_router=lambda _, value=proposal: copy.deepcopy(value)))
            elif 'resume_step' in step:
                event_pk = reports[step['resume_step']].get('event_pk')
                if not event_pk:
                    raise ValueError('resume must reference an inbound event')
                proposal = copy.deepcopy(step['proposal'])
                reports.append(resume_inbound(conn, config, event_pk=event_pk,
                    semantic_selector=selector,
                    message_router=lambda _, value=proposal: copy.deepcopy(value)))
            elif 'research_for_step' in step:
                prior = reports[step['research_for_step']]
                case_id = prior.get('result', {}).get('case_id')
                if not case_id:
                    raise ValueError('research step must reference an inbound Case')
                reports.append({'research': finish_research(
                    conn, config, case_id=case_id, documents=step['documents'],
                    selection=step['selection'],
                    clarification_reviewer=fixture_reviewer(step['clarification_review'])
                    if 'clarification_review' in step else None,
                    fail_transport=step.get('fail_transport', False))})
            else:
                prior = reports[step['case_step']]
                case_id = prior.get('result', {}).get('case_id')
                if not case_id:
                    raise ValueError('communication step must reference an inbound Case')
                reports.append({'communication': replay_communication(
                    conn, case_id=case_id, action=step['action'],
                    actor_id=config.raw['identity']['telegram_control_user_id'],
                    external_id=f'replay-control-{index}')})
        return {'steps': reports, 'model_invoked': False,
                'scope': 'fixture_driven_inbound_communication_retrieval_no_external_consumers',
                'knowledge_scope': 'synthetic_status_assumptions_not_reviewed_evidence' if knowledge else 'empty_database',
                'profile_scope': 'scenario_assumptions_not_verified_directory',
                'assumed_profiles': copy.deepcopy(profiles)}
    finally:
        conn.close()


def run_isolated_scenario(request: dict, *, timeout: float = 30) -> dict:
    return run_sandbox(
        package=Path(__file__).parent,
        site_packages=Path(sysconfig.get_paths()['purelib']), request=request,
        timeout=timeout,
        program='import json,sys\nfrom k3_support.replay_scenario import execute_scenario\n'
                'print(json.dumps(execute_scenario(json.load(sys.stdin)), ensure_ascii=False))',
    )
