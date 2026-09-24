import json
from uuid import uuid4

import pytest
from test_broker_claim import NOW, UID, queued
from test_review import active_config

from k3_support.approvals import (
    decide_approval,
    expiry_after,
    normalized_board_action,
    request_approval,
)
from k3_support.broker_board import submit
from k3_support.broker_claim_receipts import claim
from k3_support.broker_execution_contract import ExecutionContract
from k3_support.broker_start import authorize
from k3_support.ids import canonical_json, digest


@pytest.fixture
def board_request(conn, config):
    queued(conn)
    cfg = active_config(config, board=True)
    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs").fetchone()[0])
    case = payload["case_id"]
    session = case + "-board-test"
    payload["context_extra"]["board_session_id"] = session
    conn.execute("UPDATE broker_inputs SET payload_json=?", (canonical_json(payload),))
    conn.execute("UPDATE jobs SET input_digest=?", (digest(payload),))
    task = claim(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "claim", "params": {"pool": "debug"}},
                 peer_uid=UID, control_key=b"t"*32, now=NOW)["task"]
    authorize(conn, cfg, {"version": 1, "request_id": str(uuid4()), "method": "start", "params": task}, peer_uid=UID, now=NOW)
    descriptor = ExecutionContract("fixture", "https://example.com", "gpt-5.6-sol", "medium", "responses", "a"*64)
    grant = conn.execute("SELECT grant_id FROM broker_grants").fetchone()[0]
    conn.execute("INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)", (grant, descriptor.fingerprint, descriptor.provider, descriptor.model, "fixture"))
    approval_id, fingerprint, _ = request_approval(conn, approval_type="board1_lease", case_id=case,
        session_id=session, action=normalized_board_action(case, session, 30), expires_at=expiry_after(30))
    decide_approval(conn, cfg, approval_id=approval_id, approve=True, approver_user_id="owner-user",
                    approver_chat_id="owner-chat", message_id="synthetic-board", decision_text="approve board", expected_digest=fingerprint)
    conn.execute("UPDATE cases SET state='board_testing'")
    request = {"version": 1, "request_id": str(uuid4()), "method": "board_submit",
               "params": {**task, "session_id": session, "action": {"type": "reset"}}}
    return cfg, request, lambda: descriptor


def test_board_submit_queues_once_without_device_calls_or_token_storage(conn, board_request):
    cfg, request, reader = board_request
    assert submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)["state"] == "queued"
    assert submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)["state"] == "queued"
    assert conn.execute("SELECT count(*) FROM broker_board_actions").fetchone()[0] == 1
    assert request["params"]["lease_token"] not in "\n".join(conn.iterdump())
    assert conn.execute("SELECT count(*) FROM action_ledger").fetchone()[0] == 0
    with pytest.raises(ValueError, match="unresolved"):
        submit(conn, cfg, {**request, "request_id": str(uuid4())}, peer_uid=UID, contract_reader=reader, now=NOW)


@pytest.mark.parametrize("state", ["queued", "running", "unknown", "succeeded", "failed"])
def test_legacy_cleanup_cannot_reset_an_unsettled_broker_operation(conn, board_request, state):
    from k3_support.executors import BoardExecutor, ExecutorError

    cfg, request, reader = board_request
    submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    conn.execute("UPDATE broker_board_actions SET state=?", (state,))
    conn.execute("UPDATE locks SET expires_at='2020-01-01T00:00:00+00:00'")
    before = list(conn.iterdump())
    with pytest.raises(ExecutorError, match="unresolved broker"):
        BoardExecutor(cfg, runner=lambda *args: pytest.fail("must not reset unsettled device")).close_session(
            conn, case_id=request["params"]["case_id"], session_id=request["params"]["session_id"])
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("failure", ["session", "lease", "case", "grant", "feature", "action", "timeout"])
def test_board_submission_rechecks_authority_without_side_effects(conn, board_request, failure):
    cfg, request, reader = board_request
    if failure == "session":
        request["params"]["session_id"] = "another-session"
    elif failure == "lease":
        conn.execute("DELETE FROM locks WHERE lock_key='board1'")
    elif failure == "case":
        conn.execute("UPDATE cases SET state='investigating'")
    elif failure == "grant":
        conn.execute("UPDATE broker_grants SET revoked_at='fixture'")
    elif failure == "feature":
        cfg.raw["features"]["board"] = False
    elif failure == "action":
        request["params"]["action"] = {"type": "flash"}
    else:
        request["params"]["action"] = {"type": "serial_wait", "regex": ">", "timeout": True}
    before = list(conn.iterdump())
    with pytest.raises(ValueError):
        submit(conn, cfg, request, peer_uid=UID, contract_reader=reader, now=NOW)
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("state", ["queued", "running", "unknown"])
def test_worker_exit_cancels_only_undispatched_board_operations(conn, config, state):
    from test_broker_completion import add_report
    from test_broker_execution_instances import bound

    from k3_support.broker_completion import reconcile
    from k3_support.broker_execution_instances import register
    args = bound(conn, config)
    register(conn, **args)
    add_report(conn, args["grant_id"])
    grant = conn.execute("SELECT * FROM broker_grants").fetchone()
    conn.execute("INSERT INTO broker_board_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (str(uuid4()), grant["worker_uid"], args["grant_id"], "fixture-session", "fixture",
                  '{"type":"reset"}', "fixture", "fixture", state, "fixture", "fixture"))
    assert reconcile(conn, grant_id=args["grant_id"])["state"] == "unverified"
    assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == state
    conn.execute("INSERT INTO broker_service_exits VALUES(?,?,?,?,?,?)",
                 (args["grant_id"], args["invocation_id"], 1234, 1, 0, "fixture"))
    result = reconcile(conn, grant_id=args["grant_id"])
    assert result["state"] == ("review_pending" if state == "queued" else "board_cleanup_required")
    assert conn.execute("SELECT state FROM broker_board_actions").fetchone()[0] == ("cancelled" if state == "queued" else state)
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
