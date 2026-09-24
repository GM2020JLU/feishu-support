"""Durable notification intents after independent topic separation, no transport."""
from __future__ import annotations

import json

from test_conversation_context import admitted, case, item, reply
from test_routing import active_config, route_value

from k3_support.control import ControlMessage, execute_control
from k3_support.coordination import validate_outbox_fence
from k3_support.operations import reconcile
from k3_support.orchestrator import process_inbound


def separated(conn, config, *, notifications=True):
    cfg = active_config(config, codex=False)
    if not notifications:
        cfg.raw['identity']['telegram_control_chat_id'] = None
    event, _ = admitted(conn, cfg, item(group=False))
    cid = case(conn, event)
    reply(conn, cfg, event, cid)
    conn.execute("UPDATE inbound_events SET status='processed' WHERE event_pk=?", (event,))
    next_event, _ = admitted(conn, cfg, item(2, group=False, content='另一个独立问题：EC怎么更新？'))
    process_inbound(conn, event_pk=next_event, worker_id='notice-fixture', config=cfg,
                    message_router=lambda _: route_value('research', conversation_relation='standalone'))
    return cfg, cid


def notice(conn, cid):
    return conn.execute("SELECT * FROM outbox WHERE case_id=? AND json_extract(payload_json,'$.context_recheck_event_id') IS NOT NULL",
                         (cid,)).fetchone()


def test_context_notification_is_durable_idempotent_and_quiet(conn, config):
    cfg, cid = separated(conn, config)
    row = notice(conn, cid)
    assert row is not None
    for _ in range(3):
        reconcile(conn, config=cfg)
    assert conn.execute("SELECT count(*) FROM outbox WHERE idempotency_key=?", (row['idempotency_key'],)).fetchone()[0] == 1
    assert validate_outbox_fence(conn, dict(row))[0]
    detail = json.loads(row['payload_json'])
    assert '独立调试仍继续' in detail['text']
    assert conn.execute('SELECT communication_owner FROM conversation_turns WHERE case_id=?', (cid,)).fetchone()[0] == 'human'


def test_manual_claim_invalidates_prior_context_notice_without_reopening(conn, config):
    cfg, cid = separated(conn, config)
    row = notice(conn, cid)
    execute_control(conn, cfg, ControlMessage('owner-user', 'owner-chat', 'claim-context-notice', f'claim {cid}'))
    assert validate_outbox_fence(conn, dict(row)) == (False, 'context_recheck_notice_superseded')
    before = conn.execute("SELECT count(*) FROM outbox WHERE case_id=?", (cid,)).fetchone()[0]
    reconcile(conn, config=cfg)
    assert conn.execute("SELECT count(*) FROM outbox WHERE case_id=?", (cid,)).fetchone()[0] == before


def test_missing_control_identity_does_not_lose_notification_intent(conn, config):
    cfg, cid = separated(conn, config, notifications=False)
    assert notice(conn, cid) is None
    assert conn.execute("SELECT count(*) FROM case_events WHERE case_id=? AND event_type='context_recheck_required'", (cid,)).fetchone()[0] == 1
    cfg.raw['identity']['telegram_control_chat_id'] = 'owner-chat'
    reconcile(conn, config=cfg)
    assert notice(conn, cid) is not None
