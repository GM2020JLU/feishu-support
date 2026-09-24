import multiprocessing

import pytest
from test_executors import approved_board, executor_config

from k3_support.approvals import ApprovalError
from k3_support.board_operation_guard import BoardOperationBusy, hold
from k3_support.executors import BoardExecutor, ExecutionResult
from k3_support.store import create_case


def _competing_process(config, pipe):
    try:
        with hold(config):
            pipe.send("acquired")
    except BoardOperationBusy:
        pipe.send("busy")
    finally:
        pipe.close()


def test_os_lock_excludes_another_process_and_releases(config, conn):
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    with hold(config):
        process = ctx.Process(target=_competing_process, args=(config, child))
        process.start()
        try:
            assert parent.poll(5)
            assert parent.recv() == "busy"
            process.join(5)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            parent.close()
            child.close()
    with hold(config):
        pass


def test_cleanup_holds_guard_across_both_actions_and_release(conn, config):
    cfg = executor_config(config, board=True)
    case, _ = create_case(
        conn, title="guard", case_type="bug", severity="P2", confidence=0.2
    )
    approved_board(conn, cfg, case)
    calls = []
    competing = BoardExecutor(cfg, runner=lambda *args: pytest.fail("competing I/O"))

    def runner(argv, cwd, timeout):
        calls.append(argv)
        with pytest.raises(BoardOperationBusy):
            competing.close_session(conn, case_id=case, session_id="s1")
        with pytest.raises(BoardOperationBusy):
            competing.execute(
                conn, case_id=case, session_id="s1", action={"type": "reset"}
            )
        return ExecutionResult(argv, 0, "fresh marker", "")

    BoardExecutor(cfg, runner=runner).close_session(conn, case_id=case, session_id="s1")
    assert len(calls) == 2
    assert (
        conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0]
        == 0
    )


def test_old_cleanup_cannot_touch_new_owner_even_with_old_approval(conn, config):
    cfg = executor_config(config, board=True)
    case, _ = create_case(
        conn, title="old", case_type="bug", severity="P2", confidence=0.2
    )
    approved_board(conn, cfg, case)
    conn.execute(
        "UPDATE locks SET owner='new-owner',metadata_json='{\"session_id\":\"new\"}' WHERE lock_key='board1'"
    )
    board = BoardExecutor(
        cfg, runner=lambda *args: pytest.fail("must not reset new owner")
    )
    with pytest.raises(ApprovalError, match="exact board session"):
        board.close_session(conn, case_id=case, session_id="s1")


def test_symlink_guard_rejected(config, conn, tmp_path):
    target = tmp_path / "target"
    target.touch()
    config.database_path.resolve().with_suffix(".board-operation.lock").symlink_to(
        target
    )
    with pytest.raises(OSError), hold(config):
        pytest.fail("unsafe lock acquired")


@pytest.mark.parametrize("fresh", [True, False])
def test_cleanup_observer_controls_reset_order_and_release(conn, config, fresh):
    cfg = executor_config(config, board=True)
    case, _ = create_case(conn, title="armed cleanup", case_type="bug", severity="P2", confidence=.2)
    approved_board(conn, cfg, case)
    order = []
    def runner(argv, cwd, timeout):
        order.append("reset")
        assert order == ["armed", "reset"]
        return ExecutionResult(argv, 0, "control pulse", "")
    def observer(*, trigger):
        order.append("armed")
        trigger()
        order.append("observed")
        return {"ok": True, "fresh": fresh, "matched": True, "rx_seq_start": 1,
                "output": "ROM: usb download handler"}
    board = BoardExecutor(cfg, runner=runner)
    if fresh:
        results = board.close_session(conn, case_id=case, session_id="s1", observer=observer)
        assert len(results) == 2
    else:
        with pytest.raises(ValueError):
            board.close_session(conn, case_id=case, session_id="s1", observer=observer)
    assert order == ["armed", "reset", "observed"]
    assert conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0] == int(not fresh)


@pytest.mark.parametrize("revoke", [False, True])
def test_configured_observer_is_selected_and_rechecks_occupancy(conn, config, monkeypatch, revoke):
    cfg = executor_config(config, board=True)
    cfg.raw["runtime"].update(board_serial_socket="/synthetic/board1.sock", board_serial_daemon_uid=1234)
    case, _ = create_case(conn, title="configured cleanup", case_type="bug", severity="P2", confidence=.2)
    approved_board(conn, cfg, case)
    calls = []
    def observe(**kw):
        assert kw["socket_path"] == "/synthetic/board1.sock" and kw["daemon_uid"] == 1234
        kw["heartbeat"]()
        if revoke:
            conn.execute("UPDATE locks SET owner='new-owner'")
            kw["heartbeat"]()
        kw["trigger"]()
        return {"ok": True, "fresh": True, "matched": True, "rx_seq_start": 1,
                "output": "ROM: usb download handler"}
    monkeypatch.setattr("k3_support.board_serial_observer.observe_reset", observe)
    board = BoardExecutor(cfg, runner=lambda argv, cwd, timeout: calls.append(argv) or ExecutionResult(argv, 0, "pulse", ""))
    if revoke:
        with pytest.raises(ApprovalError):
            board.close_session(conn, case_id=case, session_id="s1")
        assert calls == []
    else:
        board.close_session(conn, case_id=case, session_id="s1")
        assert len(calls) == 1  # No second, late serial CLI listener.
    assert conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0] == int(revoke)
