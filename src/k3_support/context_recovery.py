"""Quiet, durable owner handoff for withdrawn replies after topic separation."""
from __future__ import annotations

import html
import json

from .conversation_context import context_fence, pending_context_rechecks
from .delivery_recovery import _notification_time, _now
from .operator_notifications import destination as notice_destination
from .operator_notifications import enqueue as enqueue_notice
from .runtime_control import capability_allowed


def current_intent(conn, event_id):
    row = conn.execute("SELECT * FROM case_events WHERE event_id=? AND event_type='context_recheck_required'",
                       (event_id,)).fetchone()
    if row is None:
        return None
    detail = json.loads(row['detail_json'])
    stamp = context_fence(conn, detail['context_id'])
    if stamp is None or any(stamp.get(key) != value for key, value in detail['context_binding'].items()):
        return None
    if stamp['communication_owner'] != 'human' or stamp['communication_mode'] != 'silent':
        return None
    case = conn.execute('SELECT state,lifecycle_round FROM cases WHERE case_id=?', (row['case_id'],)).fetchone()
    if not case or case['lifecycle_round'] != detail['lifecycle_round'] or case['state'] in {'resolved', 'cancelled', 'takeover', 'paused'}:
        return None
    if not detail.get('turns'):
        return None
    for expected in detail['turns']:
        turn = conn.execute('SELECT * FROM conversation_turns WHERE turn_id=?', (expected['turn_id'],)).fetchone()
        if (turn is None or turn['case_id'] != row['case_id']
                or turn['fence'] != expected['fence'] or turn['state'] != 'human_hold'
                or turn['communication_owner'] != 'human' or turn['communication_mode'] != 'silent'):
            return None
        if not conn.execute('SELECT 1 FROM conversation_context_members WHERE context_id=? AND event_pk=?',
                            (detail['context_id'], turn['source_event_pk'])).fetchone():
            return None
    return {'event_id': row['event_id'], 'case_id': row['case_id'], **detail}


def validate_notice(conn, row, payload):
    intent = current_intent(conn, payload.get('context_recheck_event_id'))
    if intent is None:
        return False
    primary = intent['turns'][0]
    return (row.get('case_id') == intent['case_id']
            and row.get('lifecycle_round') == intent['lifecycle_round']
            and row.get('idempotency_key') == 'context-recheck-notice:' + intent['event_id']
            and payload.get('control_turn_id') == primary['turn_id']
            and payload.get('control_fence') == primary['fence'])


def reconcile_context_rechecks(conn, config):
    if not conn.in_transaction:
        raise RuntimeError('context notification reconciliation requires a write transaction')
    if (config.mode != 'active' or not notice_destination(config)
            or not capability_allowed(conn, config, 'operator_prompt')):
        return 0
    count = 0
    for candidate in pending_context_rechecks(conn):
        intent = current_intent(conn, candidate['event_id'])
        if intent is None:
            continue
        turn = intent['turns'][0]
        _, created = enqueue_notice(conn, config, action_type='owner_decision',
            case_id=intent['case_id'],
            idempotency_key=candidate['idempotency_key'], not_before=_notification_time(config, _now()),
            payload={'case_id': intent['case_id'], 'context_recheck_event_id': intent['event_id'],
                     'control_turn_id': turn['turn_id'], 'control_fence': turn['fence'], 'parse_mode': 'HTML',
                     'text': '<b>原问题还需要处理</b>\n' + html.escape(intent['case_id']) +
                        '\n新消息已确认为另一个话题；原自动回复已撤回，没有重新发送。\n请选择接管或重新委托查证。已获准的独立调试仍继续。',
                     'buttons': [{'text': '我来回复', 'callback_data': f"k3c:c:{intent['case_id']}", 'row': 0},
                                 {'text': '查看详情', 'callback_data': f"k3c:i:{intent['case_id']}", 'row': 0}]})
        count += int(created)
    return count
