import subprocess

import pytest
from test_broker_claim import queued
from test_review import active_config

from k3_support.broker_dispatch import dispatch_one
from k3_support.broker_execution_contract import ExecutionContract


def test_manager_launch_is_fixed_noninteractive_and_uuid_only(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from k3_support.broker_dispatch import start_unit

    calls = []
    monkeypatch.setattr("k3_support.broker_dispatch.subprocess.run",
                        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0))
    request_id = str(uuid4())
    assert start_unit(request_id)
    argv, options = calls[0]
    assert argv == ["/usr/bin/systemctl", "--system", "--no-ask-password", "start", "--no-block",
                    f"k3-support-broker-worker@{request_id}.service"]
    assert options["timeout"] == 5 and "shell" not in options
    for invalid in ["../other", "--system", request_id.upper(), request_id + "\n", None]:
        with pytest.raises(ValueError):
            start_unit(invalid)
    assert len(calls) == 1


@pytest.mark.parametrize("condition", ["complete", "no_exit", "wrong_instance", "board", "new_attempt"])
def test_slot_release_requires_current_instance_and_cleanup(conn, config, condition):
    from test_broker_completion import add_report
    from test_broker_execution_instances import bound

    from k3_support.broker_dispatch import finish_observed
    from k3_support.broker_execution_instances import register

    args = bound(conn, config, board_session="unreleased" if condition == "board" else None)
    register(conn, **args)
    conn.execute("INSERT INTO broker_launches VALUES(?,'accepted','synthetic','synthetic')", (args["claim_request_id"],))
    add_report(conn, args["grant_id"])
    if condition != "no_exit":
        conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                     (args["grant_id"], "b"*32 if condition == "wrong_instance" else args["invocation_id"],
                      1234, 1, 0, "synthetic"))
    if condition == "new_attempt":
        conn.execute("UPDATE jobs SET attempt_no=attempt_no+1")
    assert finish_observed(conn) == {"finished": int(condition in ("complete", "new_attempt"))}
    before = list(conn.iterdump())
    assert finish_observed(conn) == {"finished": 0}
    assert list(conn.iterdump()) == before
    assert conn.execute("SELECT state FROM broker_launches").fetchone()[0] == ("finished" if condition in ("complete", "new_attempt") else "accepted")
    if condition == "new_attempt":
        assert tuple(conn.execute("SELECT state,attempt_no FROM jobs").fetchone()) == ("running", 2)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


@pytest.mark.parametrize("outcome", [True, False, "timeout"])
def test_durable_intent_precedes_launch_and_no_unknown_retry(conn, config, outcome):
    queued(conn)
    conn.execute("UPDATE jobs SET available_at='2000-01-01T00:00:00+00:00'")
    contract = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a"*64)
    calls = []
    def launch(request_id):
        assert not conn.in_transaction
        row = conn.execute("SELECT * FROM broker_launches").fetchone()
        assert row["state"] == "launching" and row["claim_request_id"] == request_id
        calls.append(request_id)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("synthetic", 5)
        return outcome
    result = dispatch_one(conn, active_config(config), contract_reader=lambda: contract, launch=launch)
    assert result["state"] == ("accepted" if outcome is True else "unknown")
    assert not result["execution_verified"]
    assert dispatch_one(conn, active_config(config), contract_reader=lambda: contract, launch=launch)["state"] == "occupied"
    assert len(calls) == 1
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 0


def _deployment():
    return ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a" * 64)


def _rewrite_input(conn, change):
    import json

    from k3_support.ids import canonical_json, digest

    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id='job-1'").fetchone()[0])
    change(payload)
    conn.execute("UPDATE broker_inputs SET payload_json=? WHERE job_id='job-1'", (canonical_json(payload),))
    conn.execute("UPDATE jobs SET input_digest=? WHERE job_id='job-1'", (digest(payload),))


@pytest.mark.parametrize("mismatch", ["agent", "fingerprint", "model", "reasoning"])
def test_unclaimable_task_does_not_launch_or_occupy_slot(conn, config, mismatch):
    queued(conn)
    contract = _deployment()
    def change(payload):
        payload["context_extra"]["execution"] = contract.selection()
        if mismatch == "agent":
            payload["context_extra"]["execution"]["agent"] = "hermes"
        elif mismatch == "fingerprint":
            payload["context_extra"]["execution"]["contract_fingerprint"] = "b" * 64
        else:
            payload[mismatch] = "another"
    _rewrite_input(conn, change)
    before = list(conn.iterdump())
    calls = []
    assert dispatch_one(conn, active_config(config), contract_reader=lambda: contract,
                        launch=lambda request: calls.append(request)) == {"state": "idle"}
    assert calls == [] and list(conn.iterdump()) == before


def test_invalid_snapshot_is_parked_before_worker_launch(conn, config):
    queued(conn)
    conn.execute("UPDATE jobs SET input_digest='damaged'")
    calls = []
    assert dispatch_one(conn, active_config(config), contract_reader=_deployment,
                        launch=lambda request: calls.append(request)) == {"state": "idle"}
    assert not calls
    assert tuple(conn.execute("SELECT state,attempt_no,error_class FROM jobs").fetchone()) == (
        "waiting", 0, "broker_input_invalid")
    assert conn.execute("SELECT count(*) FROM broker_launches").fetchone()[0] == 0


def test_dispatch_and_claim_skip_large_foreign_queue(conn, config):
    import json
    import os

    from k3_support.broker_claim import claim_next
    from k3_support.ids import canonical_json, digest

    queued(conn)
    conn.execute("UPDATE jobs SET priority=10")
    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0])
    payload["context_extra"]["execution"] = {"agent": "hermes", "contract_fingerprint": "b" * 64}
    # A valid job follows more foreign jobs than the bounded candidate window.
    for index in range(40):
        job_id = f"foreign-{index}"
        payload["brief"] = job_id
        conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,attempt_no,
            available_at,created_at,updated_at,context_json,lifecycle_round,priority)
            SELECT ?,case_id,job_type,state,?,attempt_no,available_at,
            created_at,updated_at,context_json,lifecycle_round,0 FROM jobs WHERE job_id='job-1'""",
            (job_id, digest(payload)))
        conn.execute("INSERT INTO broker_inputs VALUES(?,?,?)", (job_id, canonical_json(payload), "synthetic"))
    contract = _deployment()
    cfg = active_config(config)
    assert dispatch_one(conn, cfg, contract_reader=lambda: contract, launch=lambda _: True)['state'] == 'accepted'
    assert claim_next(conn, cfg, worker_uid=os.geteuid() + 1, contract=contract)['job_id'] == 'job-1'
    assert conn.execute("SELECT count(*) FROM jobs WHERE state='queued' AND attempt_no=0").fetchone()[0] == 40


def _later_task(conn):
    import json

    from k3_support.ids import canonical_json, digest

    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id='job-1'").fetchone()[0])
    payload["brief"] = "new higher priority task"
    conn.execute("""INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,attempt_no,
        available_at,created_at,updated_at,context_json,lifecycle_round,priority)
        SELECT 'job-2',case_id,job_type,'queued',?,0,available_at,
        created_at,updated_at,context_json,lifecycle_round,priority-1 FROM jobs WHERE job_id='job-1'""", (digest(payload),))
    conn.execute("INSERT INTO broker_inputs VALUES('job-2',?,'synthetic')", (canonical_json(payload),))


@pytest.mark.parametrize("change", ["priority", "cancelled", "digest", "contract", "missing_contract"])
def test_launch_claim_keeps_original_task_and_contract(conn, config, change):
    from dataclasses import replace

    from test_broker_claim import UID
    from test_broker_claim_receipts import KEY

    from k3_support.broker_claim_receipts import claim

    queued(conn)
    cfg, contract = active_config(config), _deployment()
    def launch(request_id):
        binding = conn.execute("SELECT * FROM broker_launch_bindings WHERE claim_request_id=?", (request_id,)).fetchone()
        assert binding["job_id"] == "job-1" and binding["contract_fingerprint"] == contract.fingerprint
        assert not conn.in_transaction
        return True
    dispatched = dispatch_one(conn, cfg, contract_reader=lambda: contract, launch=launch)
    _later_task(conn)
    if change == "cancelled":
        conn.execute("UPDATE jobs SET state='cancelled' WHERE job_id='job-1'")
    elif change == "digest":
        conn.execute("UPDATE jobs SET input_digest='changed' WHERE job_id='job-1'")
    elif change == "contract":
        contract = replace(contract, fingerprint="b" * 64)
    elif change == "missing_contract":
        contract = None
    value = {"version": 1, "request_id": dispatched["claim_request_id"], "method": "claim", "params": {"pool": "debug"}}
    before = list(conn.iterdump())
    if change in ("digest", "contract", "missing_contract"):
        with pytest.raises(ValueError):
            claim(conn, cfg, value, peer_uid=UID, control_key=KEY, contract_reader=lambda: contract)
        assert list(conn.iterdump()) == before
    else:
        result = claim(conn, cfg, value, peer_uid=UID, control_key=KEY, contract_reader=lambda: contract)
        if change == "cancelled":
            assert result == {"task": None}
        else:
            assert result["task"]["job_id"] == "job-1"
        # Replays retain the exact assignment or empty receipt, not the new task.
        assert claim(conn, cfg, value, peer_uid=UID, control_key=KEY, contract_reader=lambda: contract) == result
    assert tuple(conn.execute("SELECT state,attempt_no FROM jobs WHERE job_id='job-2'").fetchone()) == ("queued", 0)
    assert dispatch_one(conn, cfg, contract_reader=_deployment, launch=lambda _: pytest.fail("unexpected retry")) == {"state": "occupied"}


def test_launch_binding_is_immutable(conn, config):
    import sqlite3

    queued(conn)
    dispatch_one(conn, active_config(config), contract_reader=_deployment, launch=lambda _: True)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE broker_launch_bindings SET agent='hermes'")


def test_reclaim_requeues_claimed_never_started_launch(conn, config):
    from test_broker_claim import UID
    from test_broker_claim_receipts import KEY

    from k3_support.broker_claim_receipts import claim
    from k3_support.broker_dispatch import reclaim_unstarted_launches

    queued(conn)
    cfg, contract = active_config(config), _deployment()
    assert dispatch_one(conn, cfg, contract_reader=lambda: contract, launch=lambda _: True)["state"] == "accepted"
    request_id = conn.execute("SELECT claim_request_id FROM broker_launches").fetchone()[0]
    message = {"version": 1, "request_id": request_id, "method": "claim", "params": {"pool": "debug"}}
    assert claim(conn, cfg, message, peer_uid=UID, control_key=KEY,
                 contract_reader=lambda: contract)["task"]["job_id"] == "job-1"
    # The worker died before requesting start authorization; while the lease
    # is live nothing may be reclaimed.
    assert reclaim_unstarted_launches(conn) == {"reclaimed": 0}
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "running"
    conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    assert reclaim_unstarted_launches(conn) == {"reclaimed": 1}
    assert tuple(conn.execute("SELECT state,lease_owner,lease_expires_at FROM jobs").fetchone()) == ("queued", None, None)
    assert conn.execute("SELECT state FROM broker_launches").fetchone()[0] == "finished"
    # Idempotent, and the dispatcher slot is usable again.
    assert reclaim_unstarted_launches(conn) == {"reclaimed": 0}
    assert dispatch_one(conn, cfg, contract_reader=lambda: contract, launch=lambda _: True)["state"] == "accepted"


def test_reclaim_leaves_start_authorized_execution_alone(conn, config):
    from test_broker_execution_instances import bound

    from k3_support.broker_dispatch import reclaim_unstarted_launches

    args = bound(conn, config)
    conn.execute("INSERT INTO broker_launches VALUES(?,'accepted','synthetic','synthetic')",
                 (args["claim_request_id"],))
    conn.execute("""INSERT INTO broker_launch_bindings
        SELECT ?,job_id,input_digest,'codex',? FROM jobs""",
                 (args["claim_request_id"], "c" * 64))
    conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    assert reclaim_unstarted_launches(conn) == {"reclaimed": 0}
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "running"
