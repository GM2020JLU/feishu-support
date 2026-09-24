#!/usr/bin/env python3
"""Explicit two-call synthetic model pipeline; never live retrieval or delivery."""

import argparse
import json
import runpy
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from k3_support.config import Config, validate_config
from k3_support.db import connect, migrate
from k3_support.hermes_stdin import manifest
from k3_support.ids import digest
from k3_support.replay_research import replay_research_pipeline
from k3_support.replay_snapshot import replay_snapshot
from k3_support.routing import set_requester_profile
from k3_support.semantic import message_router_prompt, research_link_prompt


def verify(identity, *, transport=None):
    if transport is None:
        transport = runpy.run_path(str(Path(__file__).with_name('verify-hermes-bridge.py')))['verify']
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/config.example.yaml').read_text())
    raw['mode'] = 'active'
    raw['features']['auto_faq'] = True
    raw['identity'].update(feishu_owner_open_id='ou_replay_owner',
                           telegram_control_user_id='replay-owner', telegram_control_chat_id='replay-control')
    raw['scope']['technical_chat_ids'] = ['oc_replay']
    raw['scope']['auto_reply_chat_ids'] = ['oc_replay']
    observations = []

    def call(stage, prompt, reasoning):
        if (len(observations) >= 2 or any(item['stage'] == stage for item in observations)
                or any(not item['receipt_valid'] for item in observations)):
            raise ValueError('model call limit reached')
        row = {'stage': stage, 'prompt_digest': digest(prompt), 'receipt_valid': False}
        observations.append(row)
        response = transport(identity, prompt=prompt, timeout=45, reasoning=reasoning)
        row.update(receipt_valid=bool(response.get('ok')), model=response.get('model'),
                   provider=response.get('provider'), manifest_digest=response.get('manifest_digest'))
        if not row['receipt_valid']:
            raise ValueError('invalid model receipt')
        return response['result']

    with tempfile.TemporaryDirectory(prefix='codex-research-pipeline-') as directory:
        raw['paths'] = {'database': str(Path(directory) / 'synthetic.db'), 'data_dir': directory}
        config = Config(validate_config(raw), Path(directory) / 'config.yaml')
        conn = connect(config.database_path)
        try:
            migrate(conn)
            before = conn.serialize()
            with replay_snapshot(config.database_path) as memory:
                set_requester_profile(memory, requester_id='ou_replay_peer', relationship='peer',
                                      function_role='engineering', source='operator', evidence={'synthetic': True})
                event = {'source': 'feishu_user_poll', 'identity': 'user', 'external_id': 'om_pipeline',
                         'sender_id': 'ou_replay_peer', 'chat_id': 'oc_replay',
                         'occurred_at': datetime.now(UTC).isoformat(),
                         'payload': {'content': 'pico风扇太吵了，在哪改转速？给我操作文档链接就行。', 'chat_type': 'p2p'}}
                result = replay_research_pipeline(memory, config, event=event,
                    documents=[{'title': 'K3 Pico 风扇转速调节操作指南', 'url': 'https://example.com/fan',
                                'content': '合成测试资料：风扇调节章节；不是实际操作指南。'}],
                    router=lambda value: call('routing', message_router_prompt(value), 'medium'),
                    selector=lambda value: call('research_selection', research_link_prompt(value), 'low'))
            unchanged = conn.serialize() == before
        finally:
            conn.close()
    completion = (result['research'] or {}).get('completion') or {}
    owner_intent = any(row['channel'] == 'telegram' and row['action_type'] == 'owner_decision'
                       and 'https://example.com/fan' in row['payload_json'] for row in result['intentions']['outbox'])
    valid = (unchanged and len(observations) == 2 and all(item['receipt_valid'] for item in observations)
             and completion.get('state') == 'needs_owner_review'
             and completion.get('reason') == 'document_route_not_evaluated' and owner_intent)
    return {'ok': valid, 'observations': observations, 'source_unchanged': unchanged,
            'completion_state': completion.get('state'), 'completion_reason': completion.get('reason'),
            'owner_link_intent': owner_intent, 'live_messages_sent': False, 'consumers_started': False,
            'scope': 'real_model_routing_and_selection_with_synthetic_documents',
            'human_gold_reviewed': False, 'billing_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    try:
        result = verify(manifest(args.manifest))
    except Exception as error:
        result = {'ok': False, 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 1)
