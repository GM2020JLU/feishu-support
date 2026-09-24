import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_broker_connection import exchange
from test_broker_receipts import setup

from k3_support.broker_results import submit
from k3_support.executors import CODEX_RESULT_SECTIONS


def result_request(conn):
    request = setup(conn)
    request["method"] = "result"
    request["params"]["result"] = "\n".join(
        f"## {name}\n" + ("completed" if name == "status" else "not verified")
        for name in CODEX_RESULT_SECTIONS)
    return request


NOW = datetime(2026, 9, 8, 0, 30, tzinfo=UTC)


def test_result_socket_dispatch(conn, monkeypatch):
    request = result_request(conn)
    response = exchange(conn, monkeypatch, request)
    assert response["result"] == {"accepted": True, "job_id": "job-1"}
    assert conn.execute("SELECT count(*) FROM broker_results").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "running"


def test_result_is_immutable_report_not_job_completion(conn):
    request = result_request(conn)
    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    case = dict(conn.execute("SELECT * FROM cases").fetchone())
    first = submit(conn, request, peer_uid=1234, now=NOW)
    assert first == {"accepted": True, "job_id": "job-1"}
    row = conn.execute("SELECT * FROM broker_results").fetchone()
    assert json.loads(row["sections_json"])["status"] == "completed"
    assert row["result_text"] == request["params"]["result"]
    assert dict(conn.execute("SELECT * FROM jobs").fetchone()) == job
    assert dict(conn.execute("SELECT * FROM cases").fetchone()) == case
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    before = list(conn.iterdump())
    assert submit(conn, request, peer_uid=1234, now=NOW) == first
    assert list(conn.iterdump()) == before
    request["request_id"] = str(uuid4())
    request["params"]["result"] += "\nchanged"
    with pytest.raises(ValueError):
        submit(conn, request, peer_uid=1234, now=NOW)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("change", ["invalid_report", "old_round", "wrong_uid", "cancelled"])
def test_rejected_result_has_no_writes(conn, change):
    request = result_request(conn)
    uid = 1234
    if change == "invalid_report":
        request["params"]["result"] = "Everything works!"
    elif change == "old_round":
        conn.execute("UPDATE jobs SET attempt_no=2")
    elif change == "wrong_uid":
        uid = 1235
    else:
        conn.execute("UPDATE jobs SET state='cancelled'")
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        submit(conn, request, peer_uid=uid, now=NOW)
    assert list(conn.iterdump()) == before
