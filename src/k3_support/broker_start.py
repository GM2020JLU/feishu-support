"""One-time execution authorization; replay never grants a second launch."""

from datetime import UTC, datetime

from .broker_budget import charge_start, validate_running
from .broker_execution_contract import ExecutionContract
from .broker_grants import verify_bound_task
from .broker_policy import execution_allowed
from .broker_protocol import decode_request
from .broker_resources import capture
from .db import transaction
from .ids import canonical_json


def authorize(conn, config, request, *, peer_uid, now=None, observe_instance=None, contract_reader=None):
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "start":
        raise ValueError("start request required")
    contract = contract_reader() if contract_reader is not None else None
    if contract_reader is not None and type(contract) is not ExecutionContract:
        raise ValueError("control execution contract unavailable")
    if contract is not None:
        if request["params"].get("contract_fingerprint") != contract.fingerprint:
            raise ValueError("execution contract differs from control configuration")
    elif "contract_fingerprint" in request["params"]:
        raise ValueError("control execution contract unavailable")
    supplied_now = now
    now = now or datetime.now(UTC)
    with transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            raise ValueError("execution disabled")
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        from .broker_input import project
        inputs = project(conn, job_id=binding["job_id"], case_id=request["params"]["case_id"],
                         lifecycle_round=binding["lifecycle_round"], input_digest=binding["input_digest"],
                         request_id=request["request_id"])
        from .project_investigation_source import require_current

        if "investigation_source" in inputs:
            from .project_investigation_candidate import resolve as resolve_candidate
            for name, source in inputs.get('investigation_sources', {inputs['repos'][0]: inputs['investigation_source']}).items():
                require_current(config, name, source)
                resolve_candidate(conn, config, case_id=request["params"]["case_id"], repository=name, source=source)
        from .project_verifier_job import validate_job

        validate_job(conn, inputs)
        if "execution" in inputs:
            if contract is None or inputs["execution"] != contract.selection():
                raise ValueError("task coding execution differs from control contract")
        elif contract is not None and contract.agent != "codex":
            raise ValueError("legacy task requires Codex")
        if contract is not None and (inputs["model"], inputs["reasoning"]) != (contract.model, contract.reasoning):
            raise ValueError("task model differs from control contract")
        if conn.execute("SELECT 1 FROM broker_execution_starts WHERE job_id=? AND attempt_no=?",
                        (binding["job_id"], binding["attempt_no"])).fetchone():
            raise ValueError("execution already authorized; reconcile before retry")
        conn.execute("INSERT INTO broker_execution_starts VALUES(?,?,?,?,?,?)",
                     (binding["grant_id"], binding["job_id"], binding["attempt_no"],
                      request["request_id"], peer_uid, now.isoformat()))
        capture(conn, grant_id=binding["grant_id"])
        if contract is not None:
            conn.execute("INSERT INTO broker_execution_contracts VALUES(?,?,?,?,?)",
                         (binding["grant_id"], contract.fingerprint, contract.provider, contract.model, now.isoformat()))
        charge_start(conn, binding=binding, case_id=request["params"]["case_id"], contract=contract, now=now)
    if observe_instance is not None:
        # Start is committed before external observation. Failure cannot grant
        # a second launch by retry; the control supervisor must reconcile it.
        claims = conn.execute(
            "SELECT request_id FROM broker_claim_receipts WHERE peer_uid=? "
            "AND json_extract(binding_json,'$.job_id')=? "
            "AND json_extract(binding_json,'$.execution_round')=?",
            (peer_uid, binding["job_id"], binding["attempt_no"]),
        ).fetchall()
        if len(claims) != 1:
            raise ValueError("exact claim instance unavailable")
        observe_instance(conn, grant_id=binding["grant_id"], claim_request_id=claims[0]["request_id"])
        with transaction(conn):
            if not execution_allowed(conn, config, contract=contract):
                raise ValueError("execution disabled during observation")
            if contract is not None:
                current_contract = contract_reader()
                if type(current_contract) is not ExecutionContract or current_contract.fingerprint != contract.fingerprint:
                    raise ValueError("execution contract changed during observation")
                validate_running(conn, grant_id=binding["grant_id"], contract=current_contract)
            verify_bound_task(conn, request["params"], peer_uid=peer_uid,
                              now=supplied_now or datetime.now(UTC))
            if "investigation_source" in inputs:
                from .project_investigation_candidate import resolve as resolve_candidate
                for name, source in inputs.get('investigation_sources', {inputs['repos'][0]: inputs['investigation_source']}).items():
                    require_current(config, name, source)
                    resolve_candidate(conn, config, case_id=request["params"]["case_id"], repository=name, source=source)
            if not conn.execute("SELECT 1 FROM broker_execution_instances WHERE grant_id=?",
                                (binding["grant_id"],)).fetchone():
                raise ValueError("execution instance observation missing")
    from .project_verifier_job import arm

    arm(conn, config, grant_id=binding["grant_id"], inputs=inputs, case_id=request["params"]["case_id"])
    return {"accepted": True, "job_id": binding["job_id"]}
