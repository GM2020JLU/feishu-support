#!/usr/bin/env python3
"""Real bwrap, synthetic model answers; no provider or workflow consumers."""
import json
import copy
import tempfile
from pathlib import Path

import yaml

from k3_support.db import connect, migrate
from k3_support.replay_model_pipeline import run


def verify():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/config.example.yaml').read_text())
    config['mode'] = 'active'
    config['features']['auto_faq'] = True
    config['identity'].update(feishu_owner_open_id='ou_replay_owner',
        telegram_control_user_id='replay-owner', telegram_control_chat_id='replay-control')
    config['scope']['technical_chat_ids'] = ['oc_replay']
    config['scope']['auto_reply_chat_ids'] = ['oc_replay']
    results = []
    with tempfile.TemporaryDirectory(prefix='codex-sealed-review-') as directory:
        database = Path(directory) / 'synthetic.db'
        conn = connect(database)
        try:
            migrate(conn)
            before = conn.serialize()
            for approve in [False, True]:
                def router(_):
                    return {'route': 'clarify', 'confidence': .99, 'issue_type': 'bug',
                        'severity': 'P2', 'domain': 'bootloader', 'repository_hints': [],
                        'reason_codes': ['missing_version'], 'clarification_question': '现场复现使用的软件版本号是什么？',
                        'fallback_route': 'research', 'requires_owner_judgment': False,
                        'conversation_relation': 'standalone'}

                def reviewer(value):
                    if not approve:
                        return {'decision': 'reject'}
                    return {'decision': 'send', 'confidence': .98,
                        'reason_code': 'necessary_and_minimal', 'gap': 'software_version',
                        'source_quote': {'event_pk': value['messages'][0]['event_pk'],
                                         'quote': value['messages'][0]['content']},
                        'research_refs': [value['available_sources_and_checks'][0]['ref']],
                        'impact': {'operation': 'select_firmware_revision',
                            'if_answered': 'Compare changes against the exact firmware release.',
                            'if_unanswered': 'Continue generic investigation without assuming a version.',
                            'reason': 'Lookup does not identify the installed version.'},
                        'retrievability': 'not_in_available_records'}

                request = {'config': config,
                    'assumptions': {'relationship': 'peer', 'function_role': 'engineering',
                                    'observed_at': '2026-09-09T10:00:00+08:00', 'mode': 'auto'},
                    'event': {'source': 'feishu_user_poll', 'identity': 'user',
                        'external_id': 'om_review', 'sender_id': 'ou_peer', 'chat_id': 'oc_replay',
                        'occurred_at': '2026-09-09T10:00:00+08:00',
                        'payload': {'content': '升级到新版后启动卡住了', 'chat_type': 'p2p'}},
                    'documents': [{'title': 'Release notes', 'url': 'https://example.com/release',
                                   'content': 'Compare the installed firmware revision.'}]}
                result = run(database, request, router=router,
                    selector=lambda _: {'document_urls': [], 'confidence': 0},
                    clarification_reviewer=reviewer)
                stages = [item['stage'] for item in result['calls']]
                queued = any(row['action_type'] == 'clarify'
                             for row in result['result']['intentions']['outbox'])
                results.append({'approve': approve, 'stages': stages, 'question_queued': queued,
                    'ok': stages == ['routing', 'research_selection', 'clarification_review']
                          and queued is approve})
            conversation = copy.deepcopy(request)
            first = conversation.pop('event')
            first['payload']['content'] = 'Pico启动失败'
            second = copy.deepcopy(first)
            second['external_id'] += '_followup'
            second['payload'].update(content='不是Pico，是EVB', parent_id=first['external_id'])
            conversation['events'] = [first, second]
            observed = []

            def conversation_router(context):
                observed.append(json.dumps(context, ensure_ascii=False))
                return {'route': 'owner_decision', 'confidence': .99, 'issue_type': 'bug',
                    'severity': 'P2', 'domain': 'bootloader', 'repository_hints': [],
                    'reason_codes': ['requires_commitment'], 'clarification_question': None,
                    'fallback_route': 'owner_decision', 'requires_owner_judgment': True,
                    'conversation_relation': 'standalone'}

            replay = run(database, conversation, router=conversation_router,
                         selector=lambda _: {'document_urls': [], 'confidence': 0})
            turns = replay['result']['turns']
            results.append({'conversation': True, 'ok': len(turns) == 2
                and turns[0]['inbound']['result']['case_id'] == turns[1]['inbound']['result']['case_id']
                and len(observed) == 2 and 'Pico启动失败' in observed[1]
                and '不是Pico，是EVB' in observed[1]
                and [call['turn'] for call in replay['calls']] == [0, 1]})
            owner = copy.deepcopy(first)
            owner['external_id'] += '_owner'
            owner['sender_id'] = config['identity']['feishu_owner_open_id']
            owner['payload'].update(content='我来排查', parent_id=first['external_id'])
            conversation['events'] = [first, owner, second]
            observed.clear()
            controlled = run(database, conversation, router=conversation_router,
                             selector=lambda _: {'document_urls': [], 'confidence': 0})
            held = controlled['result']['turns']
            results.append({'owner_intervention': True, 'ok': len(held) == 3
                and len(observed) == 1 and len(controlled['calls']) == 1
                and all(not turn['intentions']['outbox'] and not turn['intentions']['jobs']
                        for turn in held[1:])})
            unchanged = before == conn.serialize()
        finally:
            conn.close()
    return {'ok': unchanged and all(row['ok'] for row in results), 'cases': results,
        'source_unchanged': unchanged, 'real_sandbox': True, 'real_model': False,
        'consumers_started': False}


if __name__ == '__main__':
    try:
        result = verify()
    except Exception as error:
        result = {'ok': False, 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 1)
