import sqlite3
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID
from test_broker_execution_instances import bound
from test_review import active_config

from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_remote import submit
from k3_support.broker_remote_runner import run_one


@pytest.fixture
def remote(conn, config):
    args = bound(conn, config)
    descriptor = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a" * 64)
    conn.execute("INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)",
                 (args["grant_id"], descriptor.fingerprint, descriptor.provider, descriptor.model, "synthetic"))
    cfg = active_config(config)
    task = claim(conn, cfg, {"version": 1, "request_id": args["claim_request_id"],
                            "method": "claim", "params": {"pool": "debug"}},
                 peer_uid=UID, control_key=b"t" * 32, now=NOW)["task"]
    submit(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "remote_submit",
                      "params": {**task, "remote": {"mode": "work", "repo": "u-boot", "command": "git status --short"}}},
           peer_uid=UID, contract_reader=lambda: descriptor, now=NOW)
    conn.execute("UPDATE broker_grants SET created_at='2020-01-01T00:00:00+00:00', expires_at='2099-01-01T00:00:00+00:00'")
    conn.execute("UPDATE jobs SET lease_expires_at='2099-01-01T00:00:00+00:00'")
    return cfg, lambda: descriptor


@pytest.mark.parametrize("code,state", [(0, "succeeded"), (1, "failed"), (124, "unknown"), (125, "unknown"), (255, "unknown"), (-9, "unknown")])
def test_remote_exit_receipt_and_state_commit_together(conn, remote, code, state):
    cfg, reader = remote
    def transport(**kw):
        assert not conn.in_transaction
        assert conn.execute("SELECT state FROM broker_remote_actions").fetchone()[0] == "running"
        kw["heartbeat"]()
        return {"exit_code": code, "stdout": "output", "stderr": "diagnostic"}
    result = run_one(conn, cfg, contract_reader=reader, transport=transport)
    assert result["state"] == state
    assert result["remote_cleanup_verified"] is False
    assert conn.execute("SELECT exit_code FROM broker_remote_results").fetchone()[0] == code
    assert conn.execute("SELECT state FROM broker_remote_actions").fetchone()[0] == state
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("must not repeat"))["state"] == ("occupied" if state == "unknown" else "idle")


@pytest.mark.parametrize("failure", ["receipt", "revoke", "cancel", "disconnect", "invalid"])
def test_remote_failure_never_reports_success_or_retries(conn, remote, failure):
    cfg, reader = remote
    if failure == "receipt":
        conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON broker_remote_results BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    def transport(**kw):
        if failure == "revoke":
            conn.execute("UPDATE broker_grants SET revoked_at='now'")
            kw["heartbeat"]()
        if failure == "cancel":
            conn.execute("UPDATE broker_remote_actions SET state='cancelled'")
            kw["heartbeat"]()
        if failure == "disconnect":
            raise OSError("transport unavailable")
        return None if failure == "invalid" else {"exit_code": 0, "stdout": "", "stderr": ""}
    with pytest.raises((ValueError, OSError, sqlite3.IntegrityError)):
        run_one(conn, cfg, contract_reader=reader, transport=transport)
    assert conn.execute("SELECT count(*) FROM broker_remote_results").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM broker_remote_actions").fetchone()[0] == "unknown"
    assert run_one(conn, cfg, contract_reader=reader, transport=lambda **kw: pytest.fail("must not repeat"))["state"] == "occupied"


def test_shutdown_fences_running_remote_work(conn, remote):
    import threading
    cfg, reader = remote
    stopped = threading.Event()
    def transport(**kw):
        stopped.set()
        kw["heartbeat"]()
    with pytest.raises(ValueError, match="stopping"):
        run_one(conn, cfg, contract_reader=reader, transport=transport, stop_event=stopped)
    assert conn.execute("SELECT state FROM broker_remote_actions").fetchone()[0] == "unknown"
    assert run_one(conn, cfg, contract_reader=reader, stop_event=stopped)["state"] == "stopped"


@pytest.mark.parametrize("state,code", [("queued", 0), ("cancelled", 0),
                                     ("succeeded", None), ("succeeded", 1), ("failed", 124)])
def test_conflicting_receipt_keeps_remote_consumer_occupied(conn, remote, state, code):
    cfg, reader = remote
    target = conn.execute("SELECT request_id FROM broker_remote_actions").fetchone()[0]
    conn.execute("UPDATE broker_remote_actions SET state=?", (state,))
    if code is not None:
        conn.execute("INSERT INTO broker_remote_results VALUES(?,?,?,?,?)", (target, code, "", "", "fixture"))
    before = list(conn.iterdump())
    result = run_one(conn, cfg, contract_reader=reader,
                     transport=lambda **kw: pytest.fail("conflicting exit must not launch or rerun"))
    assert result["state"] == "occupied"
    assert list(conn.iterdump()) == before
    launch = conn.execute("SELECT request_id FROM broker_claim_receipts").fetchone()[0]
    task = claim(conn, cfg, {"version": 1, "request_id": launch, "method": "claim", "params": {"pool": "debug"}},
                 peer_uid=UID, control_key=b"t" * 32, now=NOW)["task"]
    with pytest.raises(ValueError, match="remote operation unresolved"):
        submit(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "remote_submit",
                          "params": {**task, "remote": {"mode": "work", "repo": "u-boot", "command": "git status --short"}}},
               peer_uid=UID, contract_reader=reader, now=NOW)
    assert list(conn.iterdump()) == before
