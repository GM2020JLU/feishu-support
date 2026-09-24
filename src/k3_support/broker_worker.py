"""Single-task worker orchestration; the caller supplies a supervised executor."""

import json
import re
from uuid import uuid4

from .broker_protocol import ProtocolError, decode_response, encode_request


def process_executor(*, argv, cwd, env, timeout=120, heartbeat_interval=10):
    """Trusted deployment adapter, not model-selected command or environment."""
    from .broker_process import run_process

    command, environment = list(argv), dict(env)

    def execute(inputs, heartbeat):
        return run_process(argv=command, cwd=cwd, env=environment,
                           stdin=json.dumps(inputs, ensure_ascii=False, allow_nan=False).encode(),
                           heartbeat=heartbeat, timeout=timeout, heartbeat_interval=heartbeat_interval)

    return execute


def run_one(*, claim_request_id, transport, executor=None, prepare_executor=None, contract_fingerprint=None,
            executor_agent="codex", select_contract=None):
    """No retries, database access or process spawning here.

    executor(input, heartbeat) must bound its execution and stop when heartbeat
    fails. This callback contract alone is not a hostile-code isolation boundary.
    A lost start/report response requires control-side reconciliation.
    """
    if (executor is None) == (prepare_executor is None):
        raise ValueError("provide exactly one executor or preparation factory")
    def request(method, params, request_id=None):
        value = {"version": 1, "request_id": request_id or str(uuid4()), "method": method, "params": params}
        encode_request(value)
        response = transport(value)
        response = decode_response(json.dumps(response, allow_nan=False).encode(), request_id=value["request_id"])
        if not response["ok"]:
            raise ProtocolError("broker rejected worker operation")
        return response["result"]

    claimed = request("claim", {"pool": "debug"}, claim_request_id)
    if set(claimed) != {"task"}:
        raise ProtocolError("invalid claim result")
    task = claimed["task"]
    if task is None:
        return {"state": "idle"}
    # The request encoder strictly validates all binding fields and rejects extras.
    encode_request({"version": 1, "request_id": str(uuid4()), "method": "input", "params": task})
    task = dict(task)
    inputs = request("input", task)
    required = {"job_id", "input_digest", "brief", "repos", "model", "reasoning"}
    # investigation_source/verification are control-validated projections the
    # remote broker re-checks on every work request; the worker only carries them.
    optional = {"board_session_id", "execution", "investigation_source", "investigation_sources", "verification"}
    if (not required <= set(inputs) or set(inputs) - required - optional
            or inputs["job_id"] != task["job_id"] or inputs["input_digest"] != task["input_digest"]
            or any(not isinstance(inputs[key], str) or not inputs[key] for key in ("brief", "model", "reasoning"))
            or not isinstance(inputs["repos"], list) or not inputs["repos"]
            or any(not isinstance(repo, str) or not repo for repo in inputs["repos"])):
        raise ProtocolError("invalid task input projection")
    from .broker_execution_contract import ExecutionContract, validate_selection
    if select_contract is not None:
        selected = select_contract(dict(inputs))
        if type(selected) is not ExecutionContract:
            raise ProtocolError("trusted selected contract required")
        contract_fingerprint, executor_agent = selected.fingerprint, selected.agent
    if "execution" in inputs:
        selection = validate_selection(inputs["execution"])
        if selection != {"agent": executor_agent, "contract_fingerprint": contract_fingerprint}:
            raise ProtocolError("task coding execution differs from deployment")
    elif executor_agent != "codex":
        raise ProtocolError("legacy task requires Codex")
    # This is a session reference, never board approval. The board broker still
    # checks the separately approved occupancy lease on every submitted action.
    if "board_session_id" in inputs and (
            not isinstance(inputs["board_session_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", inputs["board_session_id"])):
        raise ProtocolError("invalid task board session")

    def accepted(result):
        if result.get("accepted") is not True or result.get("job_id") != task["job_id"]:
            raise ProtocolError("invalid task acknowledgement")

    def heartbeat():
        accepted(request("renew", task))

    execute = prepare_executor(dict(inputs), task=dict(task)) if prepare_executor is not None else executor
    if not callable(execute):
        raise TypeError("prepared executor required")
    heartbeat()
    start = {**task, "contract_fingerprint": contract_fingerprint} if contract_fingerprint is not None else task
    accepted(request("start", start))
    report = execute(dict(inputs), heartbeat)
    # Recheck authority after execution; never submit a late report as current work.
    heartbeat()
    accepted(request("result", {**task, "result": report}))
    return {"state": "report_received", "job_id": task["job_id"], "repair_verified": False}
