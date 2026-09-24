from uuid import uuid4

import pytest
from test_broker_input import seeded
from test_broker_start import setup
from test_broker_results import NOW
from test_review import active_config, result_text

from k3_support.broker_input import read
from k3_support.broker_protocol import ProtocolError
from k3_support.broker_renew import renew
from k3_support.broker_results import submit
from k3_support.broker_start import authorize
from k3_support.broker_worker import run_one


@pytest.mark.parametrize("board_session_id", [None, "case-1-board-canary-2"])
def test_worker_pipeline_and_replayed_claim_do_not_execute_twice(conn, config, board_session_id):
    task = setup(conn, config, board_session_id=board_session_id)["params"]
    cfg = active_config(config)
    calls = []
    def transport(request):
        method = request["method"]
        if method == "claim":
            result = {"task": task}
        elif method == "start":
            result = authorize(conn, cfg, request, peer_uid=1234, now=NOW)
        else:
            result = {"input": read, "renew": renew, "result": submit}[method](conn, request, peer_uid=1234, now=NOW)
        return {"version": 1, "request_id": request["request_id"], "ok": True, "result": result}
    def execute(inputs, heartbeat):
        assert "lease_token" not in inputs and "context_extra" not in inputs
        assert inputs.get("board_session_id") == board_session_id
        calls.append(inputs["job_id"])
        heartbeat()
        return result_text(task["case_id"])
    rid = str(uuid4())
    assert run_one(claim_request_id=rid, transport=transport, executor=execute)["state"] == "report_received"
    with pytest.raises(ValueError, match="already authorized"):
        run_one(claim_request_id=rid, transport=transport, executor=execute)
    assert calls == ["job-1"]
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "running"
    assert conn.execute("SELECT count(*) FROM broker_results").fetchone()[0] == 1


def test_worker_rejected_claim_never_calls_executor():
    def transport(request):
        return {"version": 1, "request_id": request["request_id"], "ok": False, "error": "unavailable"}
    def forbidden(*args):
        raise AssertionError("executor must not run")
    with pytest.raises(ProtocolError):
        run_one(claim_request_id=str(uuid4()), transport=transport, executor=forbidden)


@pytest.mark.parametrize("extra", [
    {"board_session_id": ""}, {"board_session_id": None},
    {"board_session_id": 1}, {"board_session_id": "x" * 257},
    {"board_session_id": "session\ncommand"}, {"board_session_id": "../session"},
    {"board_approved": True}, {"context_extra": {"board_session_id": "session"}},
])
def test_worker_rejects_invalid_or_authority_fields_before_start(conn, extra):
    task = seeded(conn)["params"]
    methods = []

    def transport(request):
        method = request["method"]
        methods.append(method)
        assert method in {"claim", "input"}
        result = {"task": task} if method == "claim" else {
            **read(conn, request, peer_uid=1234, now=NOW), **extra}
        return {"version": 1, "request_id": request["request_id"], "ok": True, "result": result}

    with pytest.raises(ProtocolError):
        run_one(claim_request_id=str(uuid4()), transport=transport,
                executor=lambda *args: pytest.fail("invalid input must not execute"))
    assert methods == ["claim", "input"]
    assert conn.execute("SELECT count(*) FROM broker_execution_starts").fetchone()[0] == 0


@pytest.mark.parametrize('joint', [False, True])
def test_worker_carries_investigation_source_through_to_executor(conn, config, joint):
    """Bug-investigation projections must not be rejected by the field gate."""
    task = setup(conn, config)["params"]
    cfg = active_config(config)
    source = {"branch": "main", "base_commit": "a" * 40, "node": "localhost",
              "version": "fixture-1", "deployment_fingerprint": "b" * 64}
    seen = []

    def transport(request):
        method = request["method"]
        if method == "claim":
            result = {"task": task}
        elif method == "start":
            result = authorize(conn, cfg, request, peer_uid=1234, now=NOW)
        elif method == "input":
            result = {**read(conn, request, peer_uid=1234, now=NOW),
                      "investigation_source": source}
            if joint:
                result['investigation_sources'] = {'first': source, 'second': source | {'base_commit': 'c'*40}}
        else:
            result = {"renew": renew, "result": submit}[method](conn, request, peer_uid=1234, now=NOW)
        return {"version": 1, "request_id": request["request_id"], "ok": True, "result": result}

    def execute(inputs, heartbeat):
        seen.append(inputs["investigation_source"])
        if joint:
            assert inputs['investigation_sources']['second']['base_commit'] == 'c'*40
        heartbeat()
        return result_text(task["case_id"])

    state = run_one(claim_request_id=str(uuid4()), transport=transport, executor=execute)["state"]
    assert state == "report_received" and seen == [source]
