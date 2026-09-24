from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from k3_support.config import is_work_time
from k3_support.coordination import communication_grace_seconds
from k3_support.knowledge_runtime import _future
from k3_support.timeutil import observed_clock, utc_now


@pytest.mark.parametrize('hour,working,grace', [(8, False, 15), (9, True, 60), (17, True, 60), (18, False, 15)])
def test_work_hours_and_reply_grace_share_observation(config, hour, working, grace):
    at = datetime.fromisoformat(f'2026-09-08T{hour:02}:00:00+08:00')
    with observed_clock(at):
        assert is_work_time(config) is working
        assert communication_grace_seconds(config) == grace
        assert utc_now() == at.astimezone(UTC)


def test_clock_restores_after_failure_and_does_not_leak_to_threads():
    before = utc_now()
    old = datetime(2000, 1, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError), observed_clock(old):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(utc_now).result() >= before
        raise RuntimeError('test')
    assert utc_now() >= before


def test_knowledge_expiry_uses_observation_not_wall_clock():
    expires = '2000-01-02T00:00:00+00:00'
    with observed_clock(datetime(2000, 1, 1, tzinfo=UTC)):
        assert _future(expires)
    with observed_clock(datetime(2000, 1, 2, tzinfo=UTC)):
        assert not _future(expires)
        assert not _future('2000-01-03T00:00:00')


def test_auto60_expires_at_exact_observed_boundary(conn, config):
    from datetime import timedelta

    from k3_support.replay_modes import switch_mode
    from k3_support.replay_snapshot import replay_snapshot
    from k3_support.runtime_control import current_global_state

    at = datetime(2000, 1, 1, tzinfo=UTC)
    with replay_snapshot(config.database_path) as snapshot:
        with observed_clock(at):
            assert switch_mode(snapshot, config, mode='auto_60', step_id='clock')['mode'] == 'auto_60'
        with observed_clock(at + timedelta(minutes=60, microseconds=-1)):
            before = current_global_state(snapshot, config)
            assert before['mode'] == 'auto_60'
        with observed_clock(at + timedelta(minutes=60)):
            expired = current_global_state(snapshot, config)
            assert expired['mode'] == 'collaborate'
            assert expired['revision'] == before['revision'] + 1
            assert current_global_state(snapshot, config)['revision'] == expired['revision']


def test_auto60_expiry_revokes_unapproved_public_reply(conn, config):
    from datetime import timedelta

    from test_routing import active_config

    from k3_support.replay_modes import switch_mode
    from k3_support.replay_snapshot import replay_snapshot
    from k3_support.runtime_control import capability_allowed

    cfg = active_config(config)
    at = datetime(2000, 1, 1, tzinfo=UTC)
    with replay_snapshot(cfg.database_path) as snapshot:
        with observed_clock(at):
            switch_mode(snapshot, cfg, mode='auto_60', step_id='permission')
            assert capability_allowed(snapshot, cfg, 'public_reply')
        with observed_clock(at + timedelta(minutes=60)):
            assert not capability_allowed(snapshot, cfg, 'public_reply')
            assert not capability_allowed(snapshot, cfg, 'public_reply', turn_id='not-delegated')
            assert capability_allowed(snapshot, cfg, 'operator_prompt')
            assert capability_allowed(snapshot, cfg, 'retrieve')


def test_claimed_reply_is_not_sent_when_auto60_expires(conn, config):
    from datetime import timedelta

    from test_runtime_control import active_config, click, issue_and_bind, make_turn

    from k3_support.coordination import bind_ai_communication
    from k3_support.db import transaction
    from k3_support.delivery import DeliveryError, claim_outbox, deliver_claimed
    from k3_support.delivery_attempts import owns_claim
    from k3_support.runtime_control import current_global_state
    from k3_support.store import enqueue_outbox

    cfg = active_config(config)
    at = utc_now()
    with observed_clock(at):
        panel = issue_and_bind(conn, cfg)
        click(conn, cfg, panel, 'global_auto_60', callback='expiry-test')
        case_id, event_pk, _ = make_turn(conn)
        with transaction(conn):
            binding = bind_ai_communication(conn, cfg, case_id=case_id, source_event_pk=event_pk)
            enqueue_outbox(conn, channel='feishu_im', action_type='reply', destination='om_runtime_question',
                payload={'text':'[AI 自动回复] 测试', 'identity':'user'}, idempotency_key='expiry-reply',
                case_id=case_id, source_event_pk=event_pk, **binding)
    row = claim_outbox(conn, worker_id='expiry-sender')
    assert row is not None and owns_claim(conn, row)
    with observed_clock(at + timedelta(minutes=60)):
        assert owns_claim(conn, row), 'test must exercise expiry, not a lost worker lease'
        with pytest.raises(DeliveryError, match='global_outbound_fence_changed'):
            deliver_claimed(conn, cfg, row, lark_runner=lambda *_: pytest.fail('expired mode sent reply'))
        assert current_global_state(conn, cfg)['mode'] == 'collaborate'
        stored = conn.execute('SELECT state FROM outbox WHERE outbox_id=?', (row['outbox_id'],)).fetchone()
        assert stored['state'] == 'cancelled'


@pytest.mark.parametrize('action,mode', [('claim', 'silent'), ('suggest_only', 'suggest_only')])
def test_auto60_activation_and_expiry_preserve_human_control(conn, config, action, mode):
    from datetime import timedelta

    from test_runtime_control import active_config, click, issue_and_bind, make_turn

    from k3_support.coordination import control_communication
    from k3_support.runtime_control import current_global_state

    cfg = active_config(config)
    case_id, _, turn = make_turn(conn)
    control_communication(conn, case_id=case_id, action=action, actor_id='owner-user', external_id='human-first')
    def ownership():
        return tuple(conn.execute('SELECT communication_owner,communication_mode,fence FROM conversation_turns WHERE turn_id=?',
                                  (turn['turn_id'],)).fetchone())
    original = ownership()
    assert original[:2] == ('human', mode)
    at = utc_now()
    with observed_clock(at):
        panel = issue_and_bind(conn, cfg)
        click(conn, cfg, panel, 'global_auto_60', callback='preserve-human')
        assert ownership() == original
    with observed_clock(at + timedelta(minutes=60)):
        assert current_global_state(conn, cfg)['mode'] == 'collaborate'
        assert ownership() == original
