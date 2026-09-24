"""Current IM context through the actual inbound worker, synthetic transport."""
from __future__ import annotations

from test_routing import active_config, route_value

from k3_support.conversation_context import admit_im_event
from k3_support.orchestrator import process_inbound


def incoming(conn, cfg, number, content, *, sender='ou_peer', parent=None):
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    payload = {'content': content, 'chat_type': 'group'}
    if parent:
        payload['parent_id'] = parent
    else:
        payload['mentions'] = [{'id': 'ou_owner'}]
    event_pk, _ = admit_im_event(conn, cfg, {
        'source': 'feishu_user_poll', 'identity': 'user', 'external_id': f'om_thread_{number}',
        'chat_id': 'oc_support', 'sender_id': sender, 'thread_id': None,
        'payload': payload, 'occurred_at': f'2026-09-07T0{number}:00:00+00:00',
    })
    assert event_pk
    return event_pk


def test_anchored_group_followup_different_sender_after_hours_uses_one_case_and_full_query(conn, config):
    cfg = active_config(config)
    first = incoming(conn, cfg, 1, 'Pico 启动失败，请查一下')
    result = process_inbound(conn, event_pk=first, worker_id='context-worker', config=cfg,
                             message_router=lambda _: route_value('research'))
    followup = incoming(conn, cfg, 7, '补充：UFS 初始化失败', sender='ou_qa', parent='om_thread_1')
    seen = []

    def router(request):
        seen.append(request)
        return route_value('research')

    second = process_inbound(conn, event_pk=followup, worker_id='context-worker', config=cfg, message_router=router)
    assert second['case_id'] == result['case_id']
    assert second['merged_followup']
    assert conn.execute('SELECT count(*) FROM cases').fetchone()[0] == 1
    assert all('Pico 启动失败' in request['message'] and 'UFS 初始化失败' in request['message'] for request in seen)


def test_new_message_while_router_waits_supersedes_only_old_claim(conn, config):
    cfg = active_config(config)
    first = incoming(conn, cfg, 1, 'Pico 启动失败')
    new_events = []

    def router(_):
        new_events.append(incoming(conn, cfg, 2, '纠正，不是Pico，是EVB', parent='om_thread_1'))
        return route_value('research')

    result = process_inbound(conn, event_pk=first, worker_id='old-worker', config=cfg, message_router=router)
    assert result['superseded']
    assert conn.execute('SELECT status FROM inbound_events WHERE event_pk=?', (first,)).fetchone()[0] == 'ignored'
    assert conn.execute('SELECT status FROM inbound_events WHERE event_pk=?', (new_events[0],)).fetchone()[0] == 'new'
    assert conn.execute('SELECT count(*) FROM cases').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


def test_backlog_old_context_event_does_not_answer_latest_question_twice(conn, config):
    cfg = active_config(config)
    first = incoming(conn, cfg, 1, 'Pico 启动失败')
    second = incoming(conn, cfg, 2, '纠正，不是Pico，是EVB', parent='om_thread_1')
    seen = []

    def router(request):
        seen.append(request['message'])
        return route_value('research')

    old = process_inbound(conn, event_pk=first, worker_id='context-worker', config=cfg, message_router=router)
    assert old['superseded'] and not seen
    current = process_inbound(conn, event_pk=second, worker_id='context-worker', config=cfg, message_router=router)
    assert current['case_id'] and len(seen) == 1
    assert conn.execute('SELECT count(*) FROM cases').fetchone()[0] == 1
