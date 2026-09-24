from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local

import pytest
from test_coding_budget import setup
from test_model_budget import policy, reserve

from k3_support import coding_budget
from k3_support import model_budget as budget
from k3_support.db import connect
from k3_support.executors import ExecutionResult
from k3_support.ids import digest


def test_policy_revision_change_cancels_unstarted_reservation(conn):
    policy(conn, 200)
    attempt = reserve(conn, amount=100)
    policy(conn, 100, revision=1)
    assert not budget.dispatch(conn, attempt["attempt_id"])
    row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    assert row["state"] == "cancelled" and row["charged"] == 0


def test_failed_predispatch_identity_never_calls_transport(conn):
    policy(conn)
    with pytest.raises(budget.BudgetError, match="identity changed"):
        budget.invoke(
            conn,
            transport=lambda: pytest.fail("transport called"),
            before_dispatch=lambda: False,
            request_id="precheck",
            case_id=None,
            provider="fixture",
            model="fixture",
            amount=50,
            input_digest=digest("input"),
        )
    row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    assert row["state"] == "cancelled" and row["charged"] == 0


def test_two_connections_cannot_start_same_coding_session_twice(
    conn, config, monkeypatch, tmp_path
):
    _, job_id = setup(conn, config)
    policy(conn, 200)
    job = dict(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    per_thread = local()

    def identity(path):
        if not getattr(per_thread, "checked", False):
            per_thread.checked = True
            barrier.wait(timeout=5)
        return {"provider": "fixture", "config_digest": "unchanged"}

    monkeypatch.setattr(coding_budget, "identity", identity)
    started = []

    def execute(_):
        db = connect(path)
        try:

            def launch():
                started.append(1)
                return ExecutionResult([], 0, "done", "")

            try:
                coding_budget.execute(
                    db,
                    job=job,
                    argv=["codex", "-m", "gpt-5.6-sol"],
                    config_path=tmp_path / "unused",
                    transport=launch,
                )
                return "started"
            except budget.BudgetError:
                return "blocked"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(execute, range(2))) == {"started", "blocked"}
    assert started == [1]
