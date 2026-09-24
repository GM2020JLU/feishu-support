#!/usr/bin/env python3
"""Real local sandbox smoke with synthetic proposals, no provider or consumers."""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from k3_support.db import connect, migrate
from k3_support.replay_history import run_snapshot_inference


def verify():
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/config.example.yaml').read_text())
    config['mode'] = 'active'
    config['identity']['feishu_owner_open_id'] = 'ou_replay_owner'
    config['identity']['telegram_control_user_id'] = 'replay-owner'
    config['identity']['telegram_control_chat_id'] = 'replay-control'
    config['scope']['technical_chat_ids'] = ['oc_replay']
    config['scope']['auto_reply_chat_ids'] = ['oc_replay']
    results = []
    with tempfile.TemporaryDirectory(prefix='codex-replay-assumptions-') as directory:
        database = Path(directory) / 'synthetic.db'
        conn = connect(database)
        try:
            migrate(conn)
            before = conn.serialize()
            for mode in ['observe', 'collaborate', 'auto_60', 'auto', 'paused', 'stopped']:
                seen = []

                def router(context):
                    seen.append(context['requester_profile'])
                    return {'route': 'owner_decision', 'confidence': .96,
                            'issue_type': 'request', 'severity': 'P3', 'domain': 'bootloader',
                            'repository_hints': [], 'reason_codes': ['requires_commitment'],
                            'clarification_question': None, 'fallback_route': None,
                            'requires_owner_judgment': True, 'conversation_relation': 'standalone'}

                assumptions = {'relationship': 'supervisor', 'function_role': 'project_manager', 'mode': mode}
                event = {'source': 'feishu_user_poll', 'identity': 'user',
                         'external_id': 'om_assumption_' + mode, 'sender_id': 'ou_replay_peer',
                         'chat_id': 'oc_replay', 'occurred_at': datetime.now(UTC).isoformat(),
                         'payload': {'content': '这个功能什么时候交付？', 'chat_type': 'p2p'}}
                result = run_snapshot_inference(database, {'config': config, 'event': event,
                                                          'assumptions': assumptions}, router=router)
                blocked = result['report']['result'].get('blocked_by_mode')
                valid = (not seen and blocked == mode) if mode in {'paused', 'stopped'} else (
                    len(seen) == 1 and seen[0]['relationship'] == 'supervisor'
                    and seen[0]['function_role'] == 'project_manager')
                results.append({'mode': mode, 'ok': bool(valid and result['assumptions'] == assumptions),
                                'synthetic_router_calls': len(seen), 'blocked_by_mode': blocked})
            unchanged = conn.serialize() == before
        finally:
            conn.close()
    return {'ok': unchanged and all(item['ok'] for item in results), 'cases': results,
            'source_unchanged': unchanged, 'real_sandbox': True, 'real_model': False,
            'consumers_started': False, 'scope': 'synthetic_role_mode_snapshot_smoke'}


if __name__ == '__main__':
    try:
        result = verify()
    except Exception as error:
        result = {'ok': False, 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 1)
