#!/usr/bin/env python3
"""Three synthetic routing requests, explicitly paid, no workflow DB or sends."""

import argparse
import json
import runpy
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from k3_support.hermes_stdin import manifest
from k3_support.ids import digest
from k3_support.routing import audience_strategy, unknown_profile, validate_route_output
from k3_support.semantic import message_router_prompt


def verify_workflow(identity, *, transport=None):
    """One synthetic inbound event through frozen-snapshot production routing.

    Never run queue consumers. The provider sees production observations only;
    expectations remain in this verifier and source state must stay unchanged.
    """
    from k3_support.db import connect, migrate
    from k3_support.replay_history import run_snapshot_inference

    if transport is None:
        transport = runpy.run_path(str(Path(__file__).with_name('verify-hermes-bridge.py')))['verify']
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'config/config.example.yaml').read_text())
    config['mode'] = 'active'
    config['identity']['feishu_owner_open_id'] = 'ou_replay_owner'
    config['identity']['telegram_control_user_id'] = 'replay-owner'
    config['identity']['telegram_control_chat_id'] = 'replay-control'
    config['scope']['technical_chat_ids'] = ['oc_replay']
    config['scope']['auto_reply_chat_ids'] = ['oc_replay']
    config['features']['codex'] = True
    event = {'source': 'feishu_user_poll', 'identity': 'user',
             'external_id': 'om_model_workflow_canary',
             'sender_id': 'ou_replay_peer', 'chat_id': 'oc_replay',
             'occurred_at': datetime.now(UTC).isoformat(),
             'payload': {'content': cases()[-1]['input']['message'], 'chat_type': 'group',
                         'mentions': [{'id': 'ou_replay_owner'}]}}
    observations = []

    def router(context):
        if observations:
            raise ValueError('unexpected second inference')
        observation = transport(identity, timeout=45, prompt=message_router_prompt(context))
        observations.append(observation)
        if not observation.get('ok'):
            raise ValueError('inference failed')
        return validate_route_output(observation['result'])

    with tempfile.TemporaryDirectory(prefix='codex-model-workflow-') as directory:
        database = Path(directory) / 'synthetic.db'
        source = connect(database)
        try:
            migrate(source)
            before = source.serialize()
            replay = run_snapshot_inference(database, {'config': config, 'event': event}, router=router)
            unchanged = source.serialize() == before
        finally:
            source.close()
    report = replay['report']
    observation = observations[0] if len(observations) == 1 else {}
    proposed = observation.get('result', {}).get('route')
    actual = report['result'].get('route', {}).get('route')
    jobs = [{'job_type': row['job_type'], 'state': row['state']} for row in report['intentions']['jobs']]
    queue_valid = (jobs == [{'job_type': 'retrieve', 'state': 'queued'}]
                   if actual == 'codex_debug' else all(job['state'] == 'queued' for job in jobs))
    return {'ok': bool(unchanged and observation.get('ok') and proposed == actual
                       and actual in {'codex_debug', 'clarify'} and queue_valid),
            'scope': 'synthetic_inbound_with_real_routing_transport_no_consumers',
            'source_unchanged': unchanged, 'model_calls': len(observations),
            'transport_receipt_valid': bool(observation.get('ok')),
            'model': observation.get('model'), 'provider': observation.get('provider'),
            'manifest_digest': observation.get('manifest_digest'),
            'routing_input_digest': replay.get('routing_input_digest'),
            'proposed_route': proposed, 'applied_route': actual, 'jobs': jobs,
            'outbox_intentions': len(report['intentions']['outbox']),
            'live_messages_sent': False, 'workers_executed': False,
            'human_gold_reviewed': False, 'billing_verified': False}


def cases():
    profile = unknown_profile('synthetic-requester')
    common = {'source': 'feishu_user_poll', 'chat_type': 'p2p',
              'requester_profile': profile, 'audience_strategy': audience_strategy(profile),
              'approved_knowledge': None, 'recent_conversation': None,
              'baseline': {'issue_type': 'investigation', 'severity': 'P3', 'domain': 'bootloader'}}
    manager = {**profile, 'relationship': 'supervisor', 'function_role': 'management'}
    return [
        {'id': 'fan-paraphrase', 'input': {**common, 'message': 'pico的风扇太吵了，在哪里设置转速？',
          'approved_knowledge': {'knowledge_id': 'synthetic-fan-guide', 'title': 'Pico 风扇调节指南', 'confidence': .99}},
         'allowed_routes': ['direct_answer']},
        {'id': 'manager-commitment', 'input': {**common, 'message': '这周五之前一定能修好吗？你给我保证一下。',
          'requester_profile': manager, 'audience_strategy': audience_strategy(manager)},
         'allowed_routes': ['owner_decision']},
        {'id': 'boot-failure', 'input': {**common, 'message': '升级后进不了系统，串口停在 U-Boot，这是哪里坏了？'},
         'allowed_routes': ['codex_debug', 'clarify']},
    ]


def verify(identity, *, transport=None, max_cases=3, case_ids=None):
    if type(max_cases) is not int or not 1 <= max_cases <= 3:
        raise ValueError('max_cases must be 1..3')
    available = {case['id']: case for case in cases()}
    if case_ids is not None and (not isinstance(case_ids, list) or not 1 <= len(case_ids) <= 3
            or any(not isinstance(name, str) or name not in available for name in case_ids)
            or len(set(case_ids)) != len(case_ids)):
        raise ValueError('select unique known canary cases')
    selected = [available[name] for name in (case_ids if case_ids is not None else list(available))][:max_cases]
    if transport is None:
        transport = runpy.run_path(str(Path(__file__).with_name('verify-hermes-bridge.py')))['verify']
    reports = []
    for case in selected:
        # Expectations stay here, outside the shared production prompt and
        # transport invocation. Stop on any failure; never retry/spend blindly.
        row = {'id': case['id'], 'input_digest': digest(case['input']),
               'receipt_valid': False, 'allowed_routes': case['allowed_routes']}
        try:
            observation = transport(identity, timeout=45, prompt=message_router_prompt(case['input']))
            row['receipt_valid'] = observation['ok']
            row['phases'] = observation.get('phases', [])
            if observation['ok']:
                proposal = validate_route_output(observation['result'])
                row.update(route=proposal['route'], matched=proposal['route'] in case['allowed_routes'],
                           model=observation['model'], provider=observation['provider'],
                           manifest_digest=observation['manifest_digest'])
            else:
                row.update(matched=False, failure_stage=observation['stage'], failure_code=observation.get('failure_code'))
        except Exception as error:  # noqa: BLE001 -- retain prior evidence, never raw errors
            row.update(matched=False, failure_stage='transport_or_validation',
                       failure_code='timeout' if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else 'invalid_or_failed_response')
        reports.append(row)
        if not row['matched']:
            break
    return {'ok': len(reports) == len(selected) and all(row['matched'] for row in reports),
            'requested_cases': len(selected),
            'cases': reports, 'evidence_class': 'synthetic', 'billing_verified': False,
            'scope': 'production_prompt_routing_only_not_workflow_or_knowledge_release',
            'human_gold_reviewed': False, 'live_messages_sent': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--workflow', action='store_true', help='One synthetic inbound snapshot; no consumers or live DB.')
    parser.add_argument('--max-cases', type=int, choices=[1, 2, 3], default=3)
    parser.add_argument('--case', action='append', choices=[case['id'] for case in cases()])
    args = parser.parse_args()
    if args.workflow and args.case:
        parser.error('--workflow cannot be combined with --case')
    try:
        report = (verify_workflow(manifest(args.manifest)) if args.workflow else
                  verify(manifest(args.manifest), max_cases=args.max_cases, case_ids=args.case))
    except Exception as error:  # noqa: BLE001 -- sanitized provider errors
        report = {'ok': False, 'error_type': type(error).__name__, 'details_redacted': True}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
