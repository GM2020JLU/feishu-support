"""Queue board work only after task and occupancy authorization; no device I/O."""

import json
from datetime import UTC, datetime

from .approvals import valid_board_lease
from .broker_budget import validate_running
from .broker_grants import verify_bound_task
from .broker_input import project
from .broker_policy import execution_allowed
from .broker_protocol import decode_request
from .db import transaction
from .ids import canonical_json, digest
from .runtime_control import capability_allowed
from .timeutil import iso_now


def authorize(conn, config, request, *, peer_uid, contract, now=None):
    binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
    return _scope(conn, config, request, binding=binding, contract=contract)


def _scope(conn, config, request, *, binding, contract):
    if not conn.in_transaction:
        raise ValueError("board authorization transaction required")
    if (not execution_allowed(conn, config, contract=contract) or not config.feature("board")
            or not capability_allowed(conn, config, "board")):
        raise ValueError("board execution disabled")
    params = request["params"]
    validate_running(conn, grant_id=binding["grant_id"], contract=contract)
    case = conn.execute("SELECT state FROM cases WHERE case_id=?", (params["case_id"],)).fetchone()
    if case["state"] != "board_testing":
        raise ValueError("case is not authorized for board testing")
    project(conn, job_id=binding["job_id"], case_id=params["case_id"], lifecycle_round=binding["lifecycle_round"],
            input_digest=binding["input_digest"], request_id=request["request_id"])
    payload = json.loads(conn.execute("SELECT payload_json FROM broker_inputs WHERE job_id=?", (binding["job_id"],)).fetchone()[0])
    if payload["context_extra"].get("board_session_id") != params["session_id"]:
        raise ValueError("board session does not match immutable task input")
    if valid_board_lease(conn, case_id=params["case_id"], session_id=params["session_id"]) is None:
        raise ValueError("board occupancy approval unavailable")
    return binding


def authorize_queued(conn, config, action, *, contract):
    current = conn.execute("SELECT * FROM broker_board_actions WHERE request_id=?", (action["request_id"],)).fetchone()
    if (not current or current["state"] not in ("queued", "running") or
            any(current[k] != action[k] for k in ("peer_uid", "grant_id", "session_id", "request_digest", "action_json", "contract_digest", "runtime_digest"))):
        raise ValueError("board action changed")
    row = conn.execute("SELECT g.*,j.case_id,j.job_type,j.state AS job_state,j.attempt_no AS job_attempt,"
                       "j.lifecycle_round AS job_round,j.input_digest AS job_input,j.lease_owner AS job_owner,"
                       "j.lease_expires_at,c.lifecycle_round AS case_round "
                       "FROM broker_grants g JOIN jobs j USING(job_id) JOIN cases c ON c.case_id=j.case_id "
                       "WHERE g.grant_id=?", (action["grant_id"],)).fetchone()
    if (not row or row["worker_uid"] != action["peer_uid"] or row["revoked_at"] is not None or
            row["job_type"] != "codex" or not row["lease_owner"] or row["job_state"] != "running" or row["attempt_no"] != row["job_attempt"] or
            row["lifecycle_round"] != row["job_round"] or row["lifecycle_round"] != row["case_round"] or
            row["input_digest"] != row["job_input"] or row["lease_owner"] != row["job_owner"]):
        raise ValueError("board task authority changed")
    now = datetime.now(UTC)
    created = datetime.fromisoformat(row["created_at"])
    if created.tzinfo is None or created > now:
        raise ValueError("board grant not yet valid")
    for field in ("expires_at", "lease_expires_at"):
        expiry = datetime.fromisoformat(row[field])
        if expiry.tzinfo is None or expiry <= now:
            raise ValueError("board task expired")
    if (contract is None or action["contract_digest"] != contract.fingerprint
            or action["runtime_digest"] != digest(config.raw["runtime"])):
        raise ValueError("board execution configuration changed")
    request = {"request_id": action["request_id"], "params": {"case_id": row["case_id"], "session_id": action["session_id"]}}
    _scope(conn, config, request, binding=dict(row), contract=contract)
    return row["case_id"]


def submit(conn, config, request, *, peer_uid, contract_reader=None, now=None):
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "board_submit":
        raise ValueError("board submission required")
    contract = contract_reader() if contract_reader else None
    with transaction(conn):
        binding = authorize(conn, config, request, peer_uid=peer_uid, contract=contract, now=now)
        fingerprint = digest(request)
        old = conn.execute("SELECT * FROM broker_board_actions WHERE request_id=?", (request["request_id"],)).fetchone()
        if old:
            if (old["peer_uid"] != peer_uid or old["grant_id"] != binding["grant_id"] or old["request_digest"] != fingerprint):
                raise ValueError("board request identity changed")
            return {"accepted": True, "request_id": request["request_id"], "state": old["state"]}
        if conn.execute("SELECT 1 FROM broker_board_actions WHERE grant_id=? AND state IN ('queued','running','unknown')",
                        (binding["grant_id"],)).fetchone():
            raise ValueError("board operation unresolved")
        stamp = iso_now()
        conn.execute("INSERT INTO broker_board_actions VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                     (request["request_id"], peer_uid, binding["grant_id"], request["params"]["session_id"], fingerprint,
                      canonical_json(request["params"]["action"]), contract.fingerprint, digest(config.raw["runtime"]), stamp, stamp))
    return {"accepted": True, "request_id": request["request_id"], "state": "queued"}


def read(conn, config, request, *, peer_uid, contract_reader=None, now=None):
    """Query this grant's bounded result, without requiring another board lease."""
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "board_read":
        raise ValueError("board read required")
    contract = contract_reader() if contract_reader else None
    with transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            raise ValueError("board result access disabled")
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        validate_running(conn, grant_id=binding["grant_id"], contract=contract)
        target = request["params"]["board_request_id"]
        row = conn.execute("SELECT a.state,r.exit_code,r.stdout,r.stderr FROM broker_board_actions a "
                           "LEFT JOIN broker_board_results r USING(request_id) "
                           "WHERE a.request_id=? AND a.grant_id=? AND a.peer_uid=?",
                           (target, binding["grant_id"], peer_uid)).fetchone()
        if row is None:
            raise ValueError("board result unavailable")
        stdout, stderr = row["stdout"] or "", row["stderr"] or ""
        offset = request["params"]["offset"]
        if offset > max(len(stdout), len(stderr)):
            raise ValueError("board offset outside output")
        end = offset + 4096
        return {"board_request_id": target, "state": row["state"], "exit_code": row["exit_code"],
                "stdout": stdout[offset:end], "stderr": stderr[offset:end],
                "next_offset": end if end < max(len(stdout), len(stderr)) else None,
                "board_cleanup_verified": False}
