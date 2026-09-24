import pytest
from test_attention_races import race
from test_model_budget import AT, policy

from k3_support import model_budget as budget
from k3_support.ids import digest


def start(conn, record):
    return budget.authorize_start(
        conn, record_start=record, request_id="synthetic-start", case_id=None,
        provider="fixture", model="fixture", amount=60,
        input_digest=digest("synthetic"), at=AT,
    )


def prepare(conn):
    policy(conn)
    conn.execute("CREATE TABLE synthetic_starts(attempt_id TEXT PRIMARY KEY)")


def record(conn, attempt):
    assert conn.in_transaction
    assert conn.execute("SELECT state FROM model_budget_attempts WHERE attempt_id=?",
                        (attempt,)).fetchone()[0] == "dispatched"
    conn.execute("INSERT INTO synthetic_starts VALUES(?)", (attempt,))


def test_start_and_charge_commit_together_and_replay_is_denied(conn):
    prepare(conn)
    result = start(conn, record)
    assert result["state"] == "dispatched" and result["charged"] == 60
    assert not conn.in_transaction
    with pytest.raises(budget.BudgetError, match="already reserved"):
        start(conn, record)
    assert conn.execute("SELECT count(*) FROM synthetic_starts").fetchone()[0] == 1


@pytest.mark.parametrize("error", [ValueError, KeyboardInterrupt])
def test_record_failure_rolls_back_charge_and_start(conn, error):
    prepare(conn)

    def failed(db, attempt):
        record(db, attempt)
        raise error()

    with pytest.raises(error):
        start(conn, failed)
    assert conn.execute("SELECT count(*) FROM synthetic_starts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 0
    assert start(conn, record)["created"]


def test_racing_starts_have_one_budget_and_one_record(conn, config):
    prepare(conn)

    def contender(db):
        try:
            start(db, record)
            return True
        except budget.BudgetError:
            return False

    assert sorted(race(config, contender, contender)) == [False, True]
    assert conn.execute("SELECT sum(charged) FROM model_budget_attempts").fetchone()[0] == 60
    assert conn.execute("SELECT count(*) FROM synthetic_starts").fetchone()[0] == 1


@pytest.mark.parametrize("precheck", [False, True])
def test_real_invoke_commits_dispatch_before_transport(conn, precheck):
    prepare(conn)
    statements = []
    conn.set_trace_callback(statements.append)

    def transport():
        assert not conn.in_transaction
        row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
        assert tuple(row) == ("dispatched", 60)
        assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
        assert sum(sql == "COMMIT" for sql in statements) == 1
        return "synthetic result"

    def identity():
        assert conn.in_transaction
        return True

    try:
        result = budget.invoke(
            conn, transport=transport, before_dispatch=identity if precheck else None,
            request_id="real-invoke", case_id=None,
            provider="fixture", model="fixture", amount=60,
            input_digest=digest("synthetic"), at=AT,
        )
    finally:
        conn.set_trace_callback(None)
    assert result["value"] == "synthetic result"
    assert result["cost_state"] == "unknown" and result["charged"] == 60
