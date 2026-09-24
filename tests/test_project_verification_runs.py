"""Broker integration for pre-bound execution; transport remains synthetic."""

import json
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID
from test_broker_execution_instances import bound
from test_project_verification import definition
from test_review import active_config

from k3_support import project_bugs as bugs
from k3_support import project_verification as plans
from k3_support import project_verification_runs as runs
from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_remote import submit
from k3_support.broker_remote_runner import run_one


@pytest.fixture
def context(conn, config):
    args = bound(conn, config)
    descriptor = ExecutionContract(
        "fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a" * 64
    )
    conn.execute(
        "INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)",
        (
            args["grant_id"],
            descriptor.fingerprint,
            descriptor.provider,
            descriptor.model,
            "synthetic",
        ),
    )
    cfg = active_config(config)
    task = claim(
        conn,
        cfg,
        {
            "version": 1,
            "request_id": args["claim_request_id"],
            "method": "claim",
            "params": {"pool": "debug"},
        },
        peer_uid=UID,
        control_key=b"t" * 32,
        now=NOW,
    )["task"]
    conn.execute(
        "UPDATE broker_grants SET created_at='2020-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00'"
    )
    conn.execute("UPDATE jobs SET lease_expires_at='2099-01-01T00:00:00+00:00'")
    case_id = conn.execute("SELECT case_id FROM jobs").fetchone()[0]
    bug = bugs.bind(
        conn,
        case_id=case_id,
        host="project.feishu.cn",
        project_key="synthetic",
        type_key="issue",
        item_id="123",
        actor="owner",
    )
    round_ = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="round",
        reason="Synthetic",
        expected_revision=1,
    )
    definition_ = definition()
    definition_["repositories"][0]["repository"] = "u-boot"
    definition_["steps"][0]["layer"] = "software_test"
    plan = plans.publish(
        conn,
        bug_id=bug["bug_id"],
        round_id=round_["round_id"],
        actor="owner",
        request_id="plan",
        expected_revision=2,
        plan=definition_,
    )
    remote = {"mode": "work", "repo": "u-boot", "command": "git status --short"}
    intent = {
        "plan_id": plan["plan_id"],
        "step_id": "function",
        "grant_id": args["grant_id"],
        "remote_request_id": str(uuid4()),
        "remote": remote,
        "actor": "owner",
        "request_id": "run",
    }
    request = {
        "version": 1,
        "request_id": intent["remote_request_id"],
        "method": "remote_submit",
        "params": {**task, "remote": remote},
    }
    return cfg, descriptor, bug, round_, plan, intent, request


def enqueue(conn, context, request=None):
    cfg, descriptor, *_, original = context
    return submit(
        conn,
        cfg,
        request or original,
        peer_uid=UID,
        contract_reader=lambda: descriptor,
        now=NOW,
    )


@pytest.mark.parametrize(
    "code,expected",
    [
        (0, "succeeded"),
        (1, "failed"),
        (124, "unknown"),
        (255, "unknown"),
        (-9, "unknown"),
    ],
)
def test_real_broker_receipt_never_becomes_verification_pass(
    conn, context, code, expected
):
    cfg, descriptor, _, round_, plan, intent, _ = context
    first = runs.prepare(conn, **intent)
    assert runs.prepare(conn, **intent) == first
    assert runs.projection(conn, plan["plan_id"])[0]["execution_state"] == "prepared"
    assert conn.execute("SELECT count(*) FROM broker_remote_actions").fetchone()[0] == 0
    enqueue(conn, context)
    assert plans.current(conn, round_["round_id"])["verification_state"] == "running"

    def transport(**kw):
        kw["heartbeat"]()
        return {"exit_code": code, "stdout": "model says passed", "stderr": ""}

    run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)
    result = runs.projection(conn, plan["plan_id"])[0]
    assert result["execution_state"] == expected
    assert result["verification_state"] == "unknown"
    assert result["oracle_verified"] is False and result["bindings_verified"] is False
    if expected in {"succeeded", "failed"}:
        assert result["receipt"]["exit_code"] == code
    else:
        assert result["receipt"] is None
    assert plans.current(conn, round_["round_id"])["verification_state"] == "unknown"


def test_cannot_adopt_preexisting_command(conn, context):
    enqueue(conn, context)
    with pytest.raises(bugs.BugConflict, match="already submitted"):
        runs.prepare(conn, **context[-2])


def test_worker_cannot_change_prebound_command(conn, context):
    runs.prepare(conn, **context[-2])
    request = context[-1]
    changed = request | {
        "params": request["params"]
        | {"remote": request["params"]["remote"] | {"command": "echo passed"}}
    }
    with pytest.raises(bugs.BugConflict, match="differs"):
        enqueue(conn, context, changed)
    assert conn.execute("SELECT count(*) FROM broker_remote_actions").fetchone()[0] == 0


def test_queued_verification_blocks_plan_replacement(conn, context):
    _, _, bug, round_, plan, intent, _ = context
    runs.prepare(conn, **intent)
    enqueue(conn, context)
    with pytest.raises(bugs.BugConflict, match="settle verification"):
        plans.publish(
            conn,
            bug_id=bug["bug_id"],
            round_id=round_["round_id"],
            actor="owner",
            request_id="replacement",
            expected_revision=3,
            plan=plan["definition"],
        )
    conn.execute("UPDATE project_bug_rounds SET execution_state='failed'")
    with pytest.raises(bugs.BugConflict, match="unsettled"):
        bugs.start_round(
            conn,
            bug_id=bug["bug_id"],
            actor="owner",
            request_id="new",
            reason="Retry",
            expected_revision=3,
        )


def test_hold_cancels_queued_execution_without_transport(conn, context):
    cfg, descriptor, _, _, plan, intent, _ = context
    runs.prepare(conn, **intent)
    enqueue(conn, context)
    conn.execute("UPDATE project_bug_rounds SET execution_state='paused'")
    result = run_one(
        conn,
        cfg,
        contract_reader=lambda: descriptor,
        transport=lambda **kw: pytest.fail("paused execution"),
    )
    assert result["state"] == "cancelled"
    assert runs.projection(conn, plan["plan_id"])[0]["receipt"] is None


def test_stripped_binding_cannot_claim_receipt(conn, context):
    runs.prepare(conn, **context[-2])
    enqueue(conn, context)
    row = conn.execute("SELECT plan_json FROM broker_remote_actions").fetchone()
    altered = json.loads(row[0])
    altered.pop("verification_run_id")
    conn.execute("UPDATE broker_remote_actions SET plan_json=?", (json.dumps(altered),))
    result = runs.projection(conn, context[4]["plan_id"])[0]
    assert result["execution_state"] == "unknown" and result["receipt"] is None


def test_missing_binding_cancels_dispatch(conn, context):
    cfg, descriptor, _, _, _, intent, _ = context
    runs.prepare(conn, **intent)
    enqueue(conn, context)
    row = conn.execute("SELECT plan_json FROM broker_remote_actions").fetchone()
    altered = json.loads(row[0])
    altered.pop("verification_run_id")
    conn.execute("UPDATE broker_remote_actions SET plan_json=?", (json.dumps(altered),))
    assert (
        run_one(
            conn,
            cfg,
            contract_reader=lambda: descriptor,
            transport=lambda **kw: pytest.fail("missing binding executed"),
        )["state"]
        == "cancelled"
    )


def test_replaced_plan_fences_prepared_intent(conn, context):
    _, _, bug, round_, plan, intent, _ = context
    runs.prepare(conn, **intent)
    plans.publish(
        conn,
        bug_id=bug["bug_id"],
        round_id=round_["round_id"],
        actor="owner",
        request_id="new",
        expected_revision=3,
        plan=plan["definition"],
    )
    with pytest.raises(bugs.BugConflict, match="no longer executable"):
        enqueue(conn, context)
    assert conn.execute("SELECT count(*) FROM broker_remote_actions").fetchone()[0] == 0


def test_verification_step_timeout_reaches_process_supervisor(conn, context):
    cfg, descriptor, _, _, _, intent, _ = context
    runs.prepare(conn, **intent)
    enqueue(conn, context)
    timeouts = []

    def transport(**kw):
        timeouts.append(kw["timeout"])
        return {"exit_code": 124, "stdout": "", "stderr": ""}

    result = run_one(conn, cfg, contract_reader=lambda: descriptor, transport=transport)
    assert timeouts == [60] and result["state"] == "unknown"
