"""Physical settlement must survive business changes without granting authority."""

import sqlite3

import pytest
from test_attention_races import race
from test_broker_completion import add_report
from test_broker_execution_instances import bound

from k3_support.broker_dispatch import finish_observed
from k3_support.broker_execution_instances import register
from k3_support.broker_resources import require_open
from k3_support.db import transaction


def exited(conn, config):
    args = bound(conn, config)
    register(conn, **args)
    conn.execute("INSERT INTO broker_launches VALUES(?,'accepted','fixture','fixture')", (args['claim_request_id'],))
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args['grant_id'], args['invocation_id'], 1234, 1, 0, 'fixture'))
    add_report(conn, args['grant_id'])
    return args


@pytest.mark.parametrize('change', [
    "UPDATE jobs SET state='orphaned'",
    "UPDATE jobs SET state='cancelled'",
    "UPDATE cases SET lifecycle_round=lifecycle_round+1",
    "UPDATE jobs SET attempt_no=attempt_no+1",
])
def test_old_resources_release_without_accepting_stale_report(conn, config, change):
    args = exited(conn, config)
    conn.execute(change)
    before = [tuple(row) for row in conn.execute('SELECT * FROM jobs')]
    assert finish_observed(conn) == {'finished': 1}
    assert [tuple(row) for row in conn.execute('SELECT * FROM jobs')] == before
    assert conn.execute('SELECT count(*) FROM case_suggestions').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    with transaction(conn), pytest.raises(ValueError, match='already settled'):
        require_open(conn, grant_id=args['grant_id'])


def test_two_connections_release_exactly_once(conn, config):
    exited(conn, config)
    conn.execute("UPDATE jobs SET state='cancelled'")
    results = race(config, finish_observed, finish_observed)
    assert sorted(result['finished'] for result in results) == [0, 1]
    assert conn.execute('SELECT settled_at FROM broker_execution_resources').fetchone()[0]


def test_cleanup_intent_and_release_share_terminal_fence(conn, config):
    args = exited(conn, config)
    conn.execute("UPDATE jobs SET state='cancelled'")
    def reserve_cleanup(other):
        with transaction(other):
            try:
                require_open(other, grant_id=args['grant_id'])
            except ValueError:
                return 'rejected'
            other.execute("INSERT INTO broker_board_cleanup VALUES(?,'unexpected','running','fixture','fixture')", (args['grant_id'],))
            return 'reserved'
    released, cleanup = race(config, finish_observed, reserve_cleanup)
    assert (released['finished'], cleanup) in ((1, 'rejected'), (0, 'reserved'))
    # A conflicting cleanup intent is never silently interpreted as no board use.
    assert not (conn.execute("SELECT 1 FROM broker_launches WHERE state='finished'").fetchone()
                and conn.execute("SELECT 1 FROM broker_board_cleanup WHERE state='running'").fetchone())


def test_report_recording_crash_does_not_reoccupy_slot_and_observer_recovers(conn, config, monkeypatch):
    from k3_support import broker_completion
    from k3_support.broker_observer_service import sweep

    exited(conn, config)
    original = broker_completion.record_codex_result
    def fail(*args, **kwargs):
        raise ValueError('injected recorder interruption')
    monkeypatch.setattr(broker_completion, 'record_codex_result', fail)
    with pytest.raises(ValueError, match='interruption'):
        finish_observed(conn)
    assert conn.execute('SELECT state FROM broker_launches').fetchone()[0] == 'finished'
    assert conn.execute('SELECT settled_at FROM broker_execution_resources').fetchone()[0]
    assert conn.execute('SELECT count(*) FROM case_suggestions').fetchone()[0] == 0
    monkeypatch.setattr(broker_completion, 'record_codex_result', original)
    sweep(conn)
    assert conn.execute('SELECT count(*) FROM case_suggestions').fetchone()[0] == 1
    sweep(conn)
    assert conn.execute('SELECT count(*) FROM case_suggestions').fetchone()[0] == 1
    assert conn.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


@pytest.mark.parametrize('damage', [None, 'missing_input', 'changed_input', 'missing_claim'])
def test_legacy_backfill_uses_only_exact_retained_evidence(conn, config, damage):
    exited(conn, config)
    # Simulate the pre-095 database after migration, not a new start without evidence.
    conn.execute('DELETE FROM broker_execution_resources')
    if damage == 'missing_input':
        conn.execute('DELETE FROM broker_inputs')
    elif damage == 'changed_input':
        conn.execute("UPDATE broker_inputs SET payload_json=json_set(payload_json,'$.brief','changed')")
    elif damage == 'missing_claim':
        conn.execute('DELETE FROM broker_claim_receipts')
    assert finish_observed(conn)['finished'] == int(damage is None)
    if damage:
        from k3_support.broker_launch_status import snapshot
        before = list(conn.iterdump())
        row = snapshot(conn)['unresolved'][0]
        assert row['resource_binding_missing']
        assert '历史资源绑定' in row['label']
        assert list(conn.iterdump()) == before


def test_resource_identity_and_terminal_marker_cannot_be_rewritten(conn, config):
    exited(conn, config)
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        conn.execute('UPDATE broker_execution_resources SET attempt_no=attempt_no+1')
    assert finish_observed(conn)['finished'] == 1
    with pytest.raises(sqlite3.IntegrityError, match='reopened'):
        conn.execute('UPDATE broker_execution_resources SET settled_at=NULL')
