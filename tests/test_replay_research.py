import pytest
from test_routing import active_config, route_value
from test_workflow_replay import group_event

from k3_support.replay_research import finish_research
from k3_support.replay_snapshot import replay_snapshot
from k3_support.workflow_replay import ReplayBoundaryError, replay_inbound


def test_research_completes_through_real_retrieval_with_fixture_links(conn, config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, 'Pico风扇怎么调节'),
                               message_router=lambda _: route_value('research'))
        before = {row[0] for row in snapshot.execute("SELECT outbox_id FROM outbox WHERE channel='feishu_im'")}
        result = finish_research(snapshot, cfg, case_id=first['result']['case_id'],
            documents=[{'title': 'Pico风扇', 'url': 'https://example.com/fan', 'content': '风扇操作说明'}],
            selection={'document_urls': ['https://example.com/fan'], 'confidence': .99})
        assert result['retrieval']['followup_eligible']
        assert result['completion']['state'] == 'needs_owner_review'
        assert result['completion']['reason'] == 'document_route_not_evaluated'
        assert snapshot.execute(
            "SELECT 1 FROM outbox WHERE channel='telegram' AND action_type='owner_decision' "
            "AND payload_json LIKE '%https://example.com/fan%'"
        ).fetchone()
        # Initial processing acknowledgements are distinct from a document reply.
        assert {row[0] for row in snapshot.execute("SELECT outbox_id FROM outbox WHERE channel='feishu_im'")} == before
    assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_research_rejects_live_database(conn, config):
    with pytest.raises(ReplayBoundaryError):
        finish_research(conn, config, case_id='missing', documents=[], selection=None)


@pytest.mark.parametrize('selection', [
    {'document_urls': [['nested']], 'confidence': .99},
    {'document_urls': [{'url': 'https://example.com/fan'}], 'confidence': .99},
    *({'document_urls': ['https://example.com/fan'], 'confidence': value}
      for value in [float('nan'), float('inf'), -1, 1.01, True, '0.99']),
    {'document_urls': ['https://example.com/not-supplied'], 'confidence': .99},
    {'document_urls': ['https://example.com/fan'] * 2, 'confidence': .99},
])
def test_invalid_model_link_selection_never_queues_document_reply(conn, config, selection):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, 'Pico风扇怎么调节'),
                               message_router=lambda _: route_value('research'))
        finish_research(snapshot, cfg, case_id=first['result']['case_id'],
            documents=[{'title': 'Pico风扇', 'url': 'https://example.com/fan', 'content': '操作说明'}],
            selection=selection)
        assert not snapshot.execute(
            "SELECT 1 FROM outbox WHERE payload_json LIKE '%https://example.com/fan%'"
        ).fetchone()


def test_lookup_failure_queues_source_investigation_without_stalling(conn, config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, '启动失败'),
                               message_router=lambda _: route_value('research'))
        result = finish_research(snapshot, cfg, case_id=first['result']['case_id'],
                                 documents=[], selection=None, fail_transport=True)
        assert result['retrieval']['state'] == 'failed'
        child_id = result['completion']['job_id']
        child = snapshot.execute('SELECT job_type,state FROM jobs WHERE job_id=?', (child_id,)).fetchone()
        assert tuple(child) == ('codex', 'queued')
        assert snapshot.execute('SELECT state FROM jobs WHERE job_id=?', (result['retrieval']['job_id'],)).fetchone()[0] == 'failed'


def test_disabled_codex_does_not_claim_debug_started(conn, config):
    cfg = active_config(config, codex=False)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, '启动失败'),
                               message_router=lambda _: route_value('research'))
        case_id = first['result']['case_id']
        result = finish_research(snapshot, cfg, case_id=case_id, documents=[],
                                 selection=None, fail_transport=True)
        assert result['completion'] is None
        assert snapshot.execute('SELECT route FROM route_decisions WHERE event_pk=?',
                                (first['event_pk'],)).fetchone()[0] == 'research'
        assert 'owner review' in snapshot.execute('SELECT next_action FROM cases WHERE case_id=?',
                                                  (case_id,)).fetchone()[0]
        assert not snapshot.execute("SELECT 1 FROM jobs WHERE job_type='codex'").fetchone()
        notices = snapshot.execute("SELECT payload_json FROM outbox WHERE action_type='owner_decision'").fetchall()
        assert len(notices) == 1 and 'Codex' in notices[0][0]
        from k3_support.orchestrator import continue_research_with_codex

        continue_research_with_codex(snapshot, cfg, case_id=case_id, query='启动失败',
            retrieval_result=None, retrieval_error='TimeoutError', retrieval_job_id=result['retrieval']['job_id'])
        assert snapshot.execute("SELECT count(*) FROM outbox WHERE action_type='owner_decision'").fetchone()[0] == 1
        from k3_support.delivery import DeliveryReceipt, claim_outbox, deliver_claimed

        sent = []

        def telegram(destination, text):
            sent.append((destination, text))
            return DeliveryReceipt('synthetic-owner-notice', {})

        claimed = claim_outbox(snapshot, worker_id='fixture-sender',
                               eligible=lambda row: row['action_type'] == 'owner_decision')
        receipt = deliver_claimed(snapshot, cfg, claimed, telegram_runner=telegram)
        assert receipt.remote_id == 'synthetic-owner-notice'
        assert len(sent) == 1 and sent[0][0] == f'telegram:{cfg.telegram_control_chat_id}'
        assert '<b>' not in sent[0][1]


@pytest.mark.parametrize('control', ['claim', 'paused', 'stopped'])
def test_late_retrieval_failure_respects_changed_control(conn, config, control):
    from k3_support.orchestrator import continue_research_with_codex
    from k3_support.replay_modes import switch_mode
    from k3_support.store import claim_jobs
    from k3_support.workflow_replay import replay_communication

    cfg = active_config(config, codex=False)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, '启动失败'),
                               message_router=lambda _: route_value('research'))
        case_id = first['result']['case_id']
        parent = claim_jobs(snapshot, 'late-fixture', job_types=('retrieve',))[0]
        if control == 'claim':
            replay_communication(snapshot, case_id=case_id, action='claim',
                                 actor_id='owner-user', external_id='late-owner-claim')
        else:
            switch_mode(snapshot, cfg, mode=control, step_id='late-control')
        before = snapshot.execute('SELECT next_action FROM cases WHERE case_id=?', (case_id,)).fetchone()[0]
        result = continue_research_with_codex(snapshot, cfg, case_id=case_id, query='启动失败',
            retrieval_result=None, retrieval_error='TimeoutError', retrieval_job_id=parent['job_id'])
        assert result is None
        assert not snapshot.execute("SELECT 1 FROM outbox WHERE action_type='owner_decision'").fetchone()
        assert not snapshot.execute("SELECT 1 FROM jobs WHERE job_type='codex'").fetchone()
        assert snapshot.execute('SELECT next_action FROM cases WHERE case_id=?', (case_id,)).fetchone()[0] == before


def test_queued_owner_notice_is_suppressed_after_takeover(conn, config):
    from k3_support.delivery import DeliverySuppressed, claim_outbox, deliver_claimed
    from k3_support.workflow_replay import replay_communication

    cfg = active_config(config, codex=False)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        first = replay_inbound(snapshot, cfg, group_event(1, '启动失败'), message_router=lambda _: route_value('research'))
        case_id = first['result']['case_id']
        finish_research(snapshot, cfg, case_id=case_id, documents=[], selection=None, fail_transport=True)
        claimed = claim_outbox(snapshot, worker_id='late-sender', eligible=lambda row: row['action_type'] == 'owner_decision')
        assert claimed
        replay_communication(snapshot, case_id=case_id, action='claim', actor_id='owner-user', external_id='owner-took-over')
        with pytest.raises(DeliverySuppressed):
            deliver_claimed(snapshot, cfg, claimed, telegram_runner=lambda *a: pytest.fail('stale reminder sent'))
