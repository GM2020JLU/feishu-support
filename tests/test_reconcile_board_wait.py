from test_executors import approved_board, executor_config

from k3_support import services
from k3_support.board_operation_guard import hold
from k3_support.executors import BoardExecutor, ExecutionResult
from k3_support.store import create_case


def run_tick(conn, cfg, monkeypatch):
    result = []
    monkeypatch.setattr(
        services, "reconcile", lambda conn, config: {"integrity": {"ok": True}}
    )
    monkeypatch.setattr("k3_support.base_sync.enqueue_dirty_entities", lambda *args: {})
    monkeypatch.setattr(
        services,
        "_run",
        lambda component, tick, interval: result.append(tick(conn, cfg)),
    )
    services.reconcile_main()
    return result[0]


def prepare(conn, config):
    cfg = executor_config(config, board=True)
    case, _ = create_case(
        conn, title="cleanup", case_type="bug", severity="P2", confidence=0.2
    )
    approved_board(conn, cfg, case)
    conn.execute(
        "UPDATE locks SET expires_at='2000-01-01T00:00:00+00:00' WHERE lock_key='board1'"
    )
    return cfg, case


def test_busy_reconcile_is_quiet_then_retries_after_release(conn, config, monkeypatch):
    cfg, case = prepare(conn, config)
    original = BoardExecutor.__init__
    calls = []

    def runner(argv, cwd, timeout):
        calls.append(argv)
        return ExecutionResult(argv, 0, "fresh marker", "")

    monkeypatch.setattr(
        BoardExecutor,
        "__init__",
        lambda self, config: original(self, config, runner=runner),
    )
    with hold(cfg):
        first = run_tick(conn, cfg, monkeypatch)
        assert first["ready"] and not first["board_cleanup_failures"]
        assert first["board_cleanup_waiting"] == [{"case_id": case, "session_id": "s1"}]
        assert not calls
        assert (
            conn.execute(
                "SELECT count(*) FROM locks WHERE lock_key='board1'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM outbox WHERE action_type='board_cleanup_failed'"
            ).fetchone()[0]
            == 0
        )
    second = run_tick(conn, cfg, monkeypatch)
    assert second["board_cleaned_sessions"] == ["s1"]
    assert not second["board_cleanup_waiting"] and len(calls) == 2
    assert (
        conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0]
        == 0
    )


def test_real_failure_still_retains_lease_and_deduplicates_alert(
    conn, config, monkeypatch
):
    cfg, _ = prepare(conn, config)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(BoardExecutor, "close_session", fail)
    for _ in range(2):
        result = run_tick(conn, cfg, monkeypatch)
        assert not result["ready"] and result["board_cleanup_failures"]
        assert not result["board_cleanup_waiting"]
    assert (
        conn.execute("SELECT count(*) FROM locks WHERE lock_key='board1'").fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM outbox WHERE action_type='board_cleanup_failed'"
        ).fetchone()[0]
        == 1
    )
