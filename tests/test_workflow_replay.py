from datetime import UTC, datetime

import pytest
from test_routing import active_config, route_value

from k3_support.replay_snapshot import replay_snapshot
from k3_support.workflow_replay import (
    ReplayBoundaryError,
    replay_communication,
    replay_inbound,
    replay_research_completion,
)


def event():
    return {'source': 'feishu_user_poll', 'identity': 'user', 'external_id': 'om_replay',
            'payload': {'content': '这个功能什么时候交付？', 'chat_type': 'p2p'},
            'occurred_at': datetime.now(UTC).isoformat(), 'sender_id': 'ou_pm', 'chat_id': 'oc_pm'}


def test_real_inbound_captures_owner_notification_without_sending(conn, config):
    cfg = active_config(config)
    with replay_snapshot(cfg.database_path) as snapshot:
        report = replay_inbound(snapshot, cfg, event(), message_router=lambda _: route_value(
            'owner_decision', issue_type='request', requires_owner_judgment=True,
            reason_codes=['requires_commitment']))
        assert report['result']['route']['route'] == 'owner_decision'
        assert report['intentions']['outbox'][0]['action_type'] == 'owner_decision'
        assert not report['intentions']['jobs']
        with pytest.raises(ValueError, match='already exists'):
            replay_inbound(snapshot, cfg, event(), message_router=None)
    assert conn.execute('SELECT count(*) FROM inbound_events').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


def test_disk_connection_rejected_before_ingestion(conn, config):
    with pytest.raises(ReplayBoundaryError, match='memory-only'):
        replay_inbound(conn, config, event(), message_router=None)
    assert conn.execute('SELECT count(*) FROM inbound_events').fetchone()[0] == 0


def test_attached_disk_database_is_rejected(conn, config, tmp_path):
    with replay_snapshot(config.database_path) as snapshot:
        snapshot.execute('ATTACH DATABASE ? AS other', (str(tmp_path / 'attached.db'),))
        with pytest.raises(ReplayBoundaryError, match='memory-only'):
            replay_inbound(snapshot, config, event(), message_router=None)
        assert snapshot.execute('SELECT count(*) FROM inbound_events').fetchone()[0] == 0


def test_raw_file_reference_not_accepted(conn, config):
    with replay_snapshot(config.database_path) as snapshot:
        incoming = event() | {'raw_artifact_path': '/private/file'}
        with pytest.raises(ValueError, match='unsupported'):
            replay_inbound(snapshot, config, incoming, message_router=None)
        assert snapshot.execute('SELECT count(*) FROM inbound_events').fetchone()[0] == 0


def group_event(number, content, *, parent=None):
    incoming = event()
    incoming.update(external_id=f'om_group_{number}', chat_id='oc_support')
    incoming['payload'] = {'content': content, 'chat_type': 'group'}
    incoming['payload'].update({'parent_id': parent} if parent else {'mentions': [{'id': 'ou_owner'}]})
    return incoming


@pytest.mark.parametrize('action', ['claim', 'suggest_only'])
def test_owner_choice_blocks_followup_router_and_new_intentions(conn, config, action):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, 'Pico启动失败'),
                               message_router=lambda _: route_value('research'))
        case_id = first['result']['case_id']
        replay_communication(snapshot, case_id=case_id, action=action,
                             actor_id='owner-user', external_id='replay-owner-choice')

        def unexpected_router(_):
            pytest.fail('AI routed a followup after human communication takeover')

        second = replay_inbound(snapshot, cfg, group_event(2, '补充：UFS初始化失败', parent='om_group_1'),
                                message_router=unexpected_router)
        assert second['result']['ignored']
        assert second['intentions'] == {'outbox': [], 'jobs': []}
    assert conn.execute('SELECT count(*) FROM cases').fetchone()[0] == 0


def test_multi_turn_replay_passes_combined_question_to_real_routing(conn, config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, 'Pico启动失败'),
                               message_router=lambda _: route_value('research'))
        seen = []

        def router(request):
            seen.append(request['message'])
            return route_value('research')

        second = replay_inbound(snapshot, cfg, group_event(2, '补充：UFS初始化失败', parent='om_group_1'),
                                message_router=router)
        assert second['result']['case_id'] == first['result']['case_id']
        assert len(seen) == 1
        assert 'Pico启动失败' in seen[0] and 'UFS初始化失败' in seen[0]
        assert snapshot.execute('SELECT count(*) FROM cases').fetchone()[0] == 1


def test_live_communication_control_rejected(conn):
    with pytest.raises(ReplayBoundaryError):
        replay_communication(conn, case_id='missing', action='claim', actor_id='owner', external_id='x')


def test_research_completion_replays_source_bound_question_without_live_writes(conn, config):
    from test_clarification_context import positive_review
    from test_clarification_research_flow import prepared_research

    with replay_snapshot(config.database_path) as snapshot:
        cfg, inbound, retrieval = prepared_research(snapshot, config)
        report = replay_research_completion(snapshot, cfg, case_id=inbound['case_id'],
            retrieval_result=retrieval, selector=None, clarification_reviewer=positive_review)
        assert report['result']['state'] == 'clarification_queued'
        assert [row['action_type'] for row in report['intentions']['outbox']] == ['clarify']
        assert not report['model_quality_verified']
        assert not report['intentions']['jobs']
    assert conn.execute('SELECT count(*) FROM inbound_events').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


def test_research_completion_rejects_live_database_before_callbacks(conn, config):
    with pytest.raises(ReplayBoundaryError, match='memory-only'):
        replay_research_completion(conn, config, case_id='missing', retrieval_result={},
                                   selector=lambda _: pytest.fail('external callback'))


@pytest.mark.parametrize('during_selection', [False, True])
def test_research_link_completion_respects_owner_takeover(conn, config, during_selection):
    from test_clarification_research_flow import prepared_research

    with replay_snapshot(config.database_path) as snapshot:
        cfg, inbound, retrieval = prepared_research(snapshot, config)
        case_id = inbound['case_id']

        def takeover():
            replay_communication(snapshot, case_id=case_id, action='claim',
                actor_id='owner-user', external_id='research-takeover')

        if not during_selection:
            takeover()
        calls = []

        def selector(request):
            calls.append(request)
            takeover()
            return {'document_urls': [request['documents'][0]['url']], 'confidence': .99}

        report = replay_research_completion(snapshot, cfg, case_id=case_id,
            retrieval_result=retrieval, selector=selector,
            clarification_reviewer=lambda _: pytest.fail('takeover must suppress questions'))
        assert report['result']['state'] == 'stale'
        assert len(calls) == int(during_selection)
        assert report['intentions'] == {'outbox': [], 'jobs': []}


def test_web_only_owner_handoff_remains_visible_without_notifications(conn, config):
    from k3_support.workbench import case_workbench_status

    cfg = active_config(config)
    cfg.raw["operator_notifications"] = {"channel": "web"}
    cfg.raw["identity"].update(control_operator_id="owner", telegram_control_chat_id=None,
                                telegram_control_user_id=None)
    with replay_snapshot(cfg.database_path) as snapshot:
        replay_inbound(snapshot, cfg, event(), message_router=lambda _: route_value(
            "owner_decision", issue_type="request", requires_owner_judgment=True,
            reason_codes=["requires_commitment"]))
        case = snapshot.execute("SELECT case_id,state FROM cases").fetchone()
        assert case["state"] == "escalated"
        assert case_workbench_status(snapshot, case_id=case["case_id"])["kind"] == "owner_decision"
        assert snapshot.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
        assert snapshot.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
