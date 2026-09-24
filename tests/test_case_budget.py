from test_broker_execution_instances import bound
from test_model_budget import policy

from k3_support import model_budget
from k3_support.case_budget import lines
from k3_support.ids import digest


def test_case_budget_is_scoped_read_only_and_keeps_unknown_charge(conn, config):
    args = bound(conn, config)
    case_id = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    policy(conn)
    attempt = model_budget.reserve(conn, request_id="synthetic", case_id=case_id,
                                   provider="private-provider-name", model="fixture", amount=60,
                                   input_digest=digest("synthetic"))
    model_budget.dispatch(conn, attempt["attempt_id"])
    model_budget.mark_unknown(conn, attempt["attempt_id"])
    conn.execute("INSERT INTO broker_budget_attempts VALUES(?,?)", (args["grant_id"], attempt["attempt_id"]))
    before = list(conn.iterdump())
    text = "\n".join(lines(conn, case_id=case_id))
    assert "费用未知，保留占用" in text and "0.00006 USD" in text
    assert "job-1" in text and "private-provider-name" not in text
    assert lines(conn, case_id="other-case") == []
    assert list(conn.iterdump()) == before
