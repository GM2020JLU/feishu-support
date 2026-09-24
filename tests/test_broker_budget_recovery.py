from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_model_budget import policy
from test_review import active_config

from k3_support import model_budget
from k3_support.broker_budget import block_unstarted
from k3_support.broker_claim_receipts import claim
from k3_support.broker_recovery import apply, preview
from k3_support.ids import digest


def test_budget_recovery_preserves_cost_and_fences_old_claim(conn, config):
    queued(conn)
    request_id = str(uuid4())
    request = {"version": 1, "request_id": request_id, "method": "claim", "params": {"pool": "debug"}}
    task = claim(conn, active_config(config), request, peer_uid=UID, now=NOW, control_key=b"t"*32)["task"]
    conn.execute("INSERT INTO broker_launches VALUES(?,'accepted','synthetic','synthetic')", (request_id,))
    policy(conn)
    model_budget.reserve(conn, request_id="prior", case_id=task["case_id"], provider="fixture", model="fixture",
                         amount=100, input_digest=digest("prior"))
    assert block_unstarted(conn, params=task, peer_uid=UID, now=NOW)
    from k3_support.execution_inventory import page

    item = page(conn, config)["items"][0]
    assert item["input_recovery_available"] and item["error_class"] == "broker_budget_blocked"
    with pytest.raises(ValueError, match="预算仍不足"):
        preview(conn, job_id=task["job_id"])
    model_budget.configure(conn, currency="USD", daily_limit=300, case_limit=300, attempt_limit=100,
                           actor_id="fixture", expected_revision=1)
    shown = preview(conn, job_id=task["job_id"])
    result = apply(conn, job_id=task["job_id"], binding_digest=shown["binding_digest"],
                   request_id=str(uuid4()), actor_id="fixture")
    assert result["accepted"] and not result["execution_authorized"]
    assert conn.execute("SELECT charged FROM model_budget_attempts").fetchone()[0] == 100
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "queued"
    assert conn.execute("SELECT state FROM broker_launches").fetchone()[0] == "finished"
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 0
    from test_broker_start_observation import synthetic_observer

    from k3_support.broker_execution_contract import ExecutionContract
    from k3_support.broker_start import authorize

    descriptor = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a"*64)
    cfg = active_config(config)
    with pytest.raises(ValueError):
        claim(conn, cfg, request, peer_uid=UID, control_key=b"t"*32, contract_reader=lambda: descriptor)
    new_task = claim(conn, cfg, {**request, "request_id": str(uuid4())}, peer_uid=UID,
                     control_key=b"t"*32, contract_reader=lambda: descriptor)["task"]
    assert new_task["execution_round"] == task["execution_round"] + 1
    assert new_task["lease_token"] != task["lease_token"]
    assert authorize(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "start",
                                "params": {**new_task, "contract_fingerprint": descriptor.fingerprint}},
                     peer_uid=UID, contract_reader=lambda: descriptor, observe_instance=synthetic_observer)["accepted"]
    assert conn.execute("SELECT sum(charged) FROM model_budget_attempts").fetchone()[0] == 200
    assert conn.execute("SELECT count(*) FROM model_budget_blocks WHERE resolved_at IS NULL").fetchone()[0] == 0
