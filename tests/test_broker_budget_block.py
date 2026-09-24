from uuid import uuid4

from test_broker_connection import exchange
from test_broker_start import setup
from test_model_budget import policy
from test_review import active_config

from k3_support import model_budget
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.ids import digest


def test_socket_budget_failure_parks_task_without_start_or_charge(conn, config, monkeypatch):
    task = setup(conn, config)["params"]
    policy(conn)
    model_budget.reserve(conn, request_id="prior", case_id=task["case_id"], provider="fixture", model="fixture",
                         amount=100, input_digest=digest("prior"))
    descriptor = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a"*64)
    request = {"version": 1, "request_id": str(uuid4()), "method": "start",
               "params": {**task, "contract_fingerprint": descriptor.fingerprint}}
    response = exchange(conn, monkeypatch, request, config=active_config(config), contract_reader=lambda: descriptor)
    assert not response["ok"]
    row = conn.execute("SELECT state,error_class,lease_owner FROM jobs").fetchone()
    assert tuple(row) == ("waiting", "broker_budget_blocked", None)
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM model_budget_attempts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM model_budget_blocks").fetchone()[0] == 1
    before = list(conn.iterdump())
    assert not exchange(conn, monkeypatch, request, config=active_config(config), contract_reader=lambda: descriptor)["ok"]
    assert list(conn.iterdump()) == before
