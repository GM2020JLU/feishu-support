import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from k3_support import model_budget as budget
from k3_support.db import connect
from k3_support.ids import digest
from k3_support.store import create_case

AT = datetime(2026, 9, 7, 12, tzinfo=UTC)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["identity", "transport"])
def test_interruption_preserves_cost_boundary(conn, interruption, stage):
    policy(conn)
    calls = []

    def check():
        if stage == "identity":
            raise interruption()
        return True

    def transport():
        calls.append(1)
        raise interruption()

    args = {
        "transport": transport, "before_dispatch": check, "request_id": "interrupted",
        "case_id": None, "provider": "fixture-provider", "model": "fixture-model",
        "amount": 60, "input_digest": digest("synthetic"), "at": AT,
    }
    with pytest.raises(interruption):
        budget.invoke(conn, **args)
    row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    assert tuple(row) == (("cancelled", 0) if stage == "identity" else ("unknown", 60))
    assert calls == ([] if stage == "identity" else [1])
    with pytest.raises(budget.BudgetError, match="already reserved"):
        budget.invoke(conn, **args)
    assert calls == ([] if stage == "identity" else [1])


def policy(conn, limit=100, revision=0):
    return budget.configure(
        conn,
        currency="USD",
        daily_limit=limit,
        case_limit=limit,
        attempt_limit=limit,
        actor_id="fixture-owner",
        expected_revision=revision,
    )


def reserve(conn, key="request", amount=60, case_id=None):
    return budget.reserve(
        conn,
        request_id=key,
        case_id=case_id,
        provider="fixture-provider",
        model="fixture-model",
        amount=amount,
        input_digest=digest("synthetic"),
        at=AT,
    )


def receipt(cost=10, key="receipt", request_id="request"):
    return {
        "receipt_id": key,
        "request_id": request_id,
        "provider": "fixture-provider",
        "model": "fixture-model",
        "currency": "USD",
        "cost": cost,
    }


def test_unconfigured_and_exhausted_budgets_never_allocate(conn):
    with pytest.raises(budget.BudgetError, match="unconfigured"):
        reserve(conn)
    policy(conn)
    first = reserve(conn)
    replay = reserve(conn)
    assert replay["created"] is False and replay["attempt_id"] == first["attempt_id"]
    with pytest.raises(budget.BudgetError, match="exhausted"):
        reserve(conn, "another")
    with pytest.raises(budget.BudgetError, match="different content"):
        reserve(conn, amount=50)


def test_two_real_connections_cannot_overreserve_daily_budget(conn, config):
    policy(conn)
    barrier = threading.Barrier(2)

    def contender(key):
        local = connect(config.database_path)
        try:
            barrier.wait(timeout=5)
            try:
                return reserve(local, key)["created"]
            except budget.BudgetError:
                return False
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(contender, ["one", "two"]))
    assert sorted(results) == [False, True]
    assert (
        conn.execute("SELECT sum(charged) FROM model_budget_attempts").fetchone()[0]
        == 60
    )


def test_unknown_cost_keeps_reservation_until_bound_receipt(conn):
    policy(conn)
    attempt = reserve(conn)["attempt_id"]
    assert budget.dispatch(conn, attempt)
    assert not budget.dispatch(conn, attempt)
    assert not budget.cancel_before_dispatch(conn, attempt)
    assert budget.mark_unknown(conn, attempt)
    assert budget.snapshot(conn)["charges"][0]["charged"] == 60
    with pytest.raises(budget.BudgetError, match="exhausted"):
        reserve(conn, "blocked")
    settled = budget.settle(conn, attempt, receipt=receipt())
    assert settled["charged"] == 10
    assert budget.settle(conn, attempt, receipt=receipt()) == settled
    assert reserve(conn, "unblocked")["created"]
    with pytest.raises(budget.BudgetError, match="rewritten"):
        budget.settle(conn, attempt, receipt=receipt(0))


def test_cancel_races_dispatch_with_one_winner(conn, config):
    policy(conn)
    attempt = reserve(conn)["attempt_id"]
    barrier = threading.Barrier(2)

    def contender(operation):
        local = connect(config.database_path)
        try:
            barrier.wait(timeout=5)
            return operation(local, attempt)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert (
            sum(
                workers.map(contender, [budget.dispatch, budget.cancel_before_dispatch])
            )
            == 1
        )
    row = conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    assert tuple(row) in {("dispatched", 60), ("cancelled", 0)}


def test_policy_changes_do_not_reset_spend_and_case_limit_crosses_days(conn):
    policy(conn)
    case_id, _ = create_case(
        conn, title="synthetic", case_type="bug", severity="P2", confidence=1
    )
    reserve(conn, case_id=case_id)
    policy(conn, limit=50, revision=1)
    with pytest.raises(budget.BudgetError, match="exhausted"):
        budget.reserve(
            conn,
            request_id="next-day",
            case_id=case_id,
            provider="fixture-provider",
            model="fixture-model",
            amount=10,
            input_digest=digest("synthetic"),
            at=datetime(2026, 9, 8, tzinfo=UTC),
        )
    assert (
        conn.execute("SELECT count(*) FROM model_budget_policy_history").fetchone()[0]
        == 2
    )
    with pytest.raises(budget.BudgetError, match="policy changed"):
        policy(conn, revision=1)
    with pytest.raises(budget.BudgetError, match="another currency"):
        budget.configure(
            conn,
            currency="EUR",
            daily_limit=100,
            case_limit=100,
            attempt_limit=100,
            actor_id="owner",
            expected_revision=2,
        )


def test_receipt_identity_and_overage_are_not_hidden(conn):
    policy(conn)
    attempt = reserve(conn)["attempt_id"]
    budget.dispatch(conn, attempt)
    with pytest.raises(budget.BudgetError, match="identity differs"):
        budget.settle(conn, attempt, receipt={**receipt(), "model": "different"})
    assert budget.settle(conn, attempt, receipt=receipt(110))["charged"] == 110
    with pytest.raises(budget.BudgetError, match="exhausted"):
        reserve(conn, "overage-blocked", amount=1)


def test_same_model_receipt_cannot_settle_another_request(conn):
    policy(conn, limit=200)
    first = reserve(conn, "first")["attempt_id"]
    second = reserve(conn, "second")["attempt_id"]
    budget.dispatch(conn, first)
    budget.dispatch(conn, second)
    bill = receipt(request_id="first")
    before = conn.serialize()
    with pytest.raises(budget.BudgetError, match="different model request"):
        budget.settle(conn, second, receipt=bill)
    assert conn.serialize() == before
    assert budget.settle(conn, first, receipt=bill)["charged"] == 10
    with pytest.raises(budget.BudgetError, match="already used"):
        budget.settle(conn, second, receipt={**bill, "request_id": "second"})
    assert (
        budget.settle(
            conn, second, receipt=receipt(key="second-bill", request_id="second")
        )["charged"]
        == 10
    )


def test_wrong_request_receipt_from_reader_retains_unknown_charge(conn):
    policy(conn)
    result = budget.invoke(
        conn,
        transport=lambda: {"answer": "synthetic"},
        trusted_receipt_reader=lambda _: receipt(request_id="another-call"),
        request_id="this-call",
        case_id=None,
        provider="fixture-provider",
        model="fixture-model",
        amount=60,
        input_digest=digest("synthetic"),
        at=AT,
    )
    assert result["cost_state"] == "unknown" and result["charged"] == 60
    assert tuple(
        conn.execute(
            "SELECT state,charged,receipt_id FROM model_budget_attempts"
        ).fetchone()
    ) == ("unknown", 60, None)


def test_trusted_correlated_receipt_settles_once_through_wrapper(conn):
    policy(conn)
    calls = []

    def transport():
        calls.append("called")
        return {"answer": "synthetic"}

    args = {
        "transport": transport,
        "trusted_receipt_reader": lambda _: receipt(request_id="this-call"),
        "request_id": "this-call",
        "case_id": None,
        "provider": "fixture-provider",
        "model": "fixture-model",
        "amount": 60,
        "input_digest": digest("synthetic"),
        "at": AT,
    }
    result = budget.invoke(conn, **args)
    assert result["cost_state"] == "settled" and result["charged"] == 10
    with pytest.raises(budget.BudgetError, match="already reserved"):
        budget.invoke(conn, **args)
    assert calls == ["called"]


def test_call_wrapper_checks_before_transport_and_does_not_trust_model_cost(conn):
    calls = []
    args = {
        "request_id": "invoke",
        "case_id": None,
        "provider": "fixture-provider",
        "model": "fixture-model",
        "amount": 60,
        "input_digest": digest("synthetic"),
        "at": AT,
    }

    def transport():
        calls.append(1)
        return {"answer": "synthetic", "cost": 0}

    with pytest.raises(budget.BudgetError, match="unconfigured"):
        budget.invoke(conn, transport=transport, **args)
    assert calls == []
    policy(conn)
    result = budget.invoke(conn, transport=transport, **args)
    assert result["cost_state"] == "unknown" and result["charged"] == 60
    with pytest.raises(budget.BudgetError, match="already reserved"):
        budget.invoke(conn, transport=transport, **args)
    with pytest.raises(budget.BudgetError, match="exhausted"):
        budget.invoke(conn, transport=transport, **{**args, "request_id": "too-much"})
    assert calls == [1]


def test_call_wrapper_timeout_retains_budget_without_exposing_exception(conn):
    policy(conn)

    def transport():
        raise TimeoutError("private prompt must never enter error message")

    with pytest.raises(budget.BudgetError, match="uncertain") as caught:
        budget.invoke(
            conn,
            transport=transport,
            request_id="timeout",
            case_id=None,
            provider="fixture-provider",
            model="fixture-model",
            amount=60,
            input_digest=digest("synthetic"),
            at=AT,
        )
    assert "private prompt" not in str(caught.value)
    assert tuple(
        conn.execute("SELECT state,charged FROM model_budget_attempts").fetchone()
    ) == ("unknown", 60)
