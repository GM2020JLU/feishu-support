import pytest
from test_broker_board import (
    board_request as board_request,  # noqa: PLC0414 - pytest fixture re-export
)

from k3_support.broker_board_cleanup import run_one
from k3_support.broker_execution_instances import register


@pytest.fixture
def cleanup(conn, board_request, monkeypatch):
    cfg, request, reader = board_request
    session = request["params"]["session_id"]
    cfg.raw["runtime"].update(board_serial_socket="/synthetic/board.sock", board_serial_daemon_uid=1234)
    conn.execute("UPDATE jobs SET context_json=json_set(context_json,'$.board_session_id',?)", (session,))
    grant = conn.execute("SELECT grant_id FROM broker_grants").fetchone()[0]
    claim = conn.execute("SELECT request_id FROM broker_claim_receipts").fetchone()[0]
    register(conn, grant_id=grant, claim_request_id=claim, invocation_id="a"*32,
             cgroup_path=f"/system.slice/k3-support-broker-worker@{claim}.service")
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)", (grant, "a"*32, 1234, 1, 0, "fixture"))
    def observer(**kwargs):
        kwargs["heartbeat"]()
        kwargs["trigger"]()
        kwargs["heartbeat"]()
        return {"ok": True, "fresh": True, "matched": True, "rx_seq_start": 12,
                "output": "ROM: usb download handler"}
    monkeypatch.setattr("k3_support.board_serial_observer.observe_reset", observer)
    return cfg, reader, grant


def test_cleanup_runs_once_after_observed_exit_and_never_replies(conn, cleanup):
    cfg, reader, _grant = cleanup
    calls = []
    def transport(**kwargs):
        calls.append(kwargs["argv"])
        kwargs["heartbeat"]()
        return {"exit_code": 0, "stdout": "pulse", "stderr": ""}
    assert run_one(conn, cfg, contract_reader=reader, transport=transport)["state"] == "board_cleaned"
    assert len(calls) == 1
    assert conn.execute("SELECT count(*) FROM locks").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM broker_board_cleanup").fetchone()[0] == "succeeded"
    assert run_one(conn, cfg, contract_reader=reader, transport=transport)["state"] == "idle"
    assert len(calls) == 1 and conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["no_exit", "new_board_owner", "missing_lock"])
def test_unbound_cleanup_never_touches_board(conn, cleanup, reason):
    cfg, reader, _ = cleanup
    if reason == "no_exit":
        conn.execute("DELETE FROM broker_service_exits")
    elif reason == "new_board_owner":
        conn.execute("UPDATE locks SET owner='another-case:new-session'")
    else:
        conn.execute("DELETE FROM locks")
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("unauthorized reset"))["state"] == "idle"
    assert conn.execute("SELECT count(*) FROM broker_board_cleanup").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    "UPDATE jobs SET context_json='{}'",
    "UPDATE jobs SET attempt_no=attempt_no+1",
    "UPDATE jobs SET state='orphaned'",
])
def test_old_execution_can_clean_its_own_board_after_business_changes(conn, cleanup, change):
    cfg, reader, grant = cleanup
    conn.execute(change)
    before = tuple(conn.execute("SELECT state,attempt_no,context_json FROM jobs").fetchone())
    calls = []
    def transport(**kwargs):
        calls.append(kwargs['argv'])
        kwargs['heartbeat']()
        return {'exit_code': 0, 'stdout': 'pulse', 'stderr': ''}
    assert run_one(conn, cfg, contract_reader=reader, transport=transport)['state'] == 'board_cleaned'
    assert len(calls) == 1
    assert tuple(conn.execute("SELECT state,attempt_no,context_json FROM jobs").fetchone()) == before
    assert conn.execute("SELECT count(*) FROM locks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_lost_cleanup_response_is_durable_unknown_without_retry(conn, cleanup):
    cfg, reader, _ = cleanup
    def lost(**kwargs):
        raise OSError("lost response")
    with pytest.raises(OSError):
        run_one(conn, cfg, contract_reader=reader, transport=lost)
    assert conn.execute("SELECT state FROM broker_board_cleanup").fetchone()[0] == "unknown"
    assert conn.execute("SELECT count(*) FROM locks").fetchone()[0] == 1
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("no retry"))["state"] == "cleanup_unknown"
    from k3_support.executors import BoardExecutor, ExecutorError
    case = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    session = conn.execute("SELECT session_id FROM broker_board_cleanup").fetchone()[0]
    with pytest.raises(ExecutorError, match="legacy retry"):
        BoardExecutor(cfg, runner=lambda *a: pytest.fail("legacy retry")).close_session(conn, case_id=case, session_id=session)


def test_board_service_dispatches_cleanup_before_normal_actions(conn, config, monkeypatch):
    from k3_support.broker_board_service import run_one as consume
    monkeypatch.setattr("k3_support.broker_board_service.run_cleanup", lambda *a, **kw: {"state": "board_cleaned"})
    monkeypatch.setattr("k3_support.broker_board_service.run_action", lambda *a, **kw: pytest.fail("no overlapping action"))
    assert consume(conn, config, contract_reader=lambda: None)["state"] == "board_cleaned"


def test_successful_cleanup_then_completion_produces_review_not_reply(conn, cleanup):
    from test_broker_completion import add_report

    from k3_support.broker_completion import reconcile

    cfg, reader, grant = cleanup
    add_report(conn, grant)
    assert reconcile(conn, grant_id=grant)["state"] == "board_cleanup_required"
    assert run_one(conn, cfg, contract_reader=reader,
                   transport=lambda **kw: {"exit_code": 0, "stdout": "pulse", "stderr": ""})["state"] == "board_cleaned"
    result = reconcile(conn, grant_id=grant)
    assert result["state"] == "review_pending" and not result["repair_verified"] and not result["reply_sent"]
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("damage", [None, "other_attempt", "old_receipt", "missing_receipt", "missing_exit"])
def test_post_release_crash_recovers_only_exact_durable_evidence(conn, cleanup, monkeypatch, damage):
    from k3_support.executors import BoardExecutor

    cfg, reader, _ = cleanup
    original = BoardExecutor.close_session
    def crash_after_release(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError("crash after release before final intent update")
    monkeypatch.setattr(BoardExecutor, "close_session", crash_after_release)
    with pytest.raises(OSError):
        run_one(conn, cfg, contract_reader=reader,
                transport=lambda **kw: {"exit_code": 0, "stdout": "pulse", "stderr": ""})
    assert conn.execute("SELECT count(*) FROM locks").fetchone()[0] == 0
    if damage == "other_attempt":
        conn.execute("UPDATE action_ledger SET action_key=replace(action_key,':cleanup:broker_',':cleanup:unrelated_')")
    elif damage == "old_receipt":
        conn.execute("UPDATE action_ledger SET finished_at='2020-01-01T00:00:00+00:00'")
    elif damage == "missing_receipt":
        conn.execute("DELETE FROM action_ledger WHERE action_key=(SELECT action_key FROM action_ledger LIMIT 1)")
    elif damage == "missing_exit":
        conn.execute("DELETE FROM broker_service_exits")
    result = run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("recovery must not reset"))
    assert result["state"] == ("board_cleanup_recovered" if damage is None else "cleanup_unknown")
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
