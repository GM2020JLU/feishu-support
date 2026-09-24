import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_broker_grant_schema import grant
from test_broker_task_binding import seed

from k3_support.broker_receipts import execute


def setup(conn):
    params = seed(conn)
    grant(conn, token_digest=hashlib.sha256(b"broker-secret").hexdigest())
    params["lease_token"] = "broker-secret"
    return {"version": 1, "request_id": str(uuid4()), "method": "renew", "params": params}


def test_receipt_replay_does_not_execute_again(conn):
    request = setup(conn)
    calls = []

    def handler(db, binding, value):
        calls.append(value["request_id"])
        return {"accepted": True, "job_id": binding["job_id"]}

    args = {"peer_uid": 1234, "handler": handler, "now": datetime(2026, 9, 8, 0, 30, tzinfo=UTC)}
    first = execute(conn, request, **args)
    assert execute(conn, request, **args) == first
    assert len(calls) == 1
    with pytest.raises(ValueError, match="binding changed"):
        execute(conn, {**request, "params": {**request["params"], "execution_round": 2}}, **args)
    assert "broker-secret" not in "\n".join(conn.iterdump())


def test_invalid_receipt_rolls_back_handler_write(conn):
    request = setup(conn)
    before = list(conn.iterdump())

    def handler(db, binding, value):
        db.execute("UPDATE jobs SET state='cancelled' WHERE job_id='job-1'")
        return {"accepted": True, "job_id": "job-1", "token": "must not persist"}

    with pytest.raises(ValueError, match="receipt"):
        execute(conn, request, peer_uid=1234, handler=handler, now=datetime(2026, 9, 8, 0, 30, tzinfo=UTC))
    assert list(conn.iterdump()) == before
