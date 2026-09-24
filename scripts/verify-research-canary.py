#!/usr/bin/env python3
"""Two explicit synthetic research-selector requests; never sends messages."""
import argparse
import json
import runpy
from pathlib import Path

from k3_support.hermes_stdin import manifest
from k3_support.ids import digest
from k3_support.routing import audience_strategy, unknown_profile
from k3_support.semantic import clarification_review_prompt, research_link_prompt


def verify_debug_unknown(identity, *, transport=None):
    """One synthetic unresolved caller report; no DB, board or delivery runner."""
    from k3_support.decision import REQUIRED_FIELDS
    from k3_support.review import _review_prompt

    if transport is None:
        transport = runpy.run_path(str(Path(__file__).with_name('verify-hermes-bridge.py')))['verify']
    bundle = {'review_id': 'synthetic-debug', 'sections': {'status': 'completed',
        'root_cause': 'Unknown. board1 boots normally, but the caller still reports boot failure.',
        'verification': 'Only local build and local boot were observed.',
        'risks': 'Caller firmware, board revision and boot medium are unknown.'},
        'checks': [{'layer': 'build', 'result': 'passed', 'environment': 'synthetic-local'}],
        'evidence_ids': ['synthetic-build']}
    case = {'case_id': 'K3-SYNTHETIC', 'state': 'investigating', 'version': 3,
            'severity': 'P2', 'title': 'Caller boot failure remains unexplained'}
    prompt = _review_prompt(bundle, case, {'type': 'feishu_reply', 'message_id': 'om_synthetic', 'identity': 'user'})
    report = {'ok': False, 'input_digest': digest(prompt), 'scope': 'synthetic_debug_review_prompt_only',
              'live_messages_sent': False, 'board_used': False, 'human_gold_reviewed': False}
    try:
        observation = transport(identity, timeout=45, reasoning='medium', prompt=prompt)
        report['receipt_valid'] = bool(observation.get('ok'))
        if not observation.get('ok'):
            return report
        result = observation['result']
        report['ok'] = (isinstance(result, dict) and set(result) == REQUIRED_FIELDS
            and result['decision_id'] == 'hermes-review-synthetic-debug'
            and result['case_id'] == case['case_id'] and type(result['expected_case_version']) is int
            and result['expected_case_version'] == 3 and result['intent'] in {'wait', 'escalate'}
            and result['reply_draft'] is None and result['proposed_actions'] == []
            and result['evidence_ids'] == [] and type(result['confidence']) in {int, float}
            and 0 <= result['confidence'] <= 1
            and all(isinstance(result[k], list) and all(isinstance(x, str) for x in result[k])
                    for k in ('facts', 'inferences', 'unknowns')) and bool(result['unknowns']))
        report.update(model=observation['model'], provider=observation['provider'],
                      manifest_digest=observation['manifest_digest'])
    except Exception:  # noqa: BLE001 -- never print provider text or retry
        report['failure_code'] = 'transport_or_validation_failed'
    return report


def verify_clarification(identity, *, transport=None):
    """Negative synthetic reviews only; does not create an approval record."""
    if transport is None:
        transport = runpy.run_path(str(Path(__file__).with_name('verify-hermes-bridge.py')))['verify']
    profile = unknown_profile('synthetic-requester')
    rows = []
    for name, problem, question in [
        ('already-provided', 'Pico板，软件版本v1.2.3，升级后启动失败。', '软件版本号是什么？'),
        ('too-broad', '升级后启动失败。', '请把全部源码、完整日志和所有配置文件都发来。'),
    ]:
        value = {'original_problem': problem, 'question': question,
                 'messages': [{'event_pk': 'synthetic-event', 'content': problem}],
                 'known_information': {}, 'already_asked': [],
                 'available_sources_and_checks': [], 'retrieval_status': 'not_checked_or_not_current',
                 'gap': 'software_version', 'requester_profile': profile,
                 'audience_strategy': audience_strategy(profile)}
        row = {'id': name, 'input_digest': digest(value), 'ok': False}
        try:
            observation = transport(identity, timeout=30, reasoning='medium',
                                    prompt=clarification_review_prompt(value))
            row['receipt_valid'] = bool(observation.get('ok'))
            if observation.get('ok'):
                from k3_support.clarification_context import (
                    REVIEW_FIELDS,
                    review_allows_question,
                )
                result = observation['result']
                confidence = result.get('confidence')
                row['ok'] = (set(result) == REVIEW_FIELDS and result.get('decision') == 'reject'
                             and type(confidence) in {int, float} and 0 <= confidence <= 1
                             and result.get('reason_code') in {'already_provided', 'too_broad', 'insufficient_context',
                                                               'retrievable', 'social_risk', 'does_not_change_action'}
                             and not review_allows_question(value, result, .92))
                row.update(model=observation['model'], provider=observation['provider'],
                           manifest_digest=observation['manifest_digest'])
        except Exception:  # noqa: BLE001 -- no private exception details
            row['failure_code'] = 'transport_or_validation_failed'
        rows.append(row)
        if not row['ok']:
            break
    return {'ok': len(rows) == 2 and all(row['ok'] for row in rows), 'cases': rows,
            'scope': 'synthetic_negative_clarification_reviews_only',
            'live_messages_sent': False, 'approval_created': False, 'human_gold_reviewed': False}


def verify(identity, *, transport=None):
    if transport is None:
        transport = runpy.run_path(str(Path(__file__).with_name('verify-hermes-bridge.py')))['verify']
    profile = unknown_profile('synthetic-requester')
    documents = [{'title': 'K3 Pico 风扇转速调节操作指南', 'url': 'https://example.com/fan'},
                 {'title': 'K3 EC 固件升级操作指南', 'url': 'https://example.com/ec'}]
    rows = []
    for name, query, expected in [
        ('fan-paraphrase', 'pico风扇太吵了，在哪改转速？', ['https://example.com/fan']),
        ('unrelated', '公司出差费用怎么报销？', []),
    ]:
        value = {'query': query, 'documents': documents, 'requester_profile': profile,
                 'audience_strategy': audience_strategy(profile)}
        row = {'id': name, 'input_digest': digest(value), 'ok': False}
        try:
            observation = transport(identity, timeout=30, reasoning='low', prompt=research_link_prompt(value))
            row['receipt_valid'] = bool(observation.get('ok'))
            if observation.get('ok'):
                result = observation['result']
                confidence = result.get('confidence')
                valid = (set(result) == {'document_urls', 'confidence'}
                         and type(confidence) in {int, float} and 0 <= confidence <= 1)
                row['ok'] = bool(valid and result['document_urls'] == expected
                                 and (confidence >= .9 if expected else confidence == 0))
                row.update(model=observation['model'], provider=observation['provider'],
                           manifest_digest=observation['manifest_digest'])
            else:
                row['failure_code'] = observation.get('failure_code')
        except Exception:  # noqa: BLE001 -- no provider text or retries
            row['failure_code'] = 'transport_or_validation_failed'
        rows.append(row)
        if not row['ok']:
            break
    return {'ok': len(rows) == 2 and all(row['ok'] for row in rows), 'cases': rows,
            'scope': 'synthetic_production_research_prompt_only', 'live_messages_sent': False,
            'human_gold_reviewed': False, 'billing_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--clarification', action='store_true')
    mode.add_argument('--debug-unknown', action='store_true')
    args = parser.parse_args()
    verifier = verify_debug_unknown if args.debug_unknown else verify_clarification if args.clarification else verify
    result = verifier(manifest(args.manifest))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ok'] else 2)
