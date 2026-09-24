"""Control-only remote execution consumer; never reads worker-selected config."""

import json
from datetime import UTC, datetime
from pathlib import Path

from .broker_budget import validate_running
from .broker_policy import execution_allowed
from .broker_process import run_process
from .broker_remote_state import UNSETTLED
from .db import transaction
from .execution_transport import matches, plan_argv
from .remote_guard import receipt_directory
from .store import EXECUTABLE_CASE_STATES
from .timeutil import iso_now


def _live(conn, config, action, contract):
    current = conn.execute("SELECT * FROM broker_remote_actions WHERE request_id=?",
                           (action["request_id"],)).fetchone()
    if (not current or current["state"] not in ("queued", "running")
            or any(current[key] != action[key] for key in ("grant_id", "peer_uid", "plan_json", "request_digest"))):
        raise ValueError("remote action changed")
    if not execution_allowed(conn, config, contract=contract):
        raise ValueError("remote execution disabled")
    row = conn.execute("SELECT g.*,j.case_id,j.state AS job_state,j.attempt_no AS job_attempt,"
                       "j.lifecycle_round AS job_round,j.input_digest AS job_input,j.lease_owner AS job_owner,"
                       "j.lease_expires_at,c.lifecycle_round AS case_round,c.state AS case_state "
                       "FROM broker_grants g JOIN jobs j USING(job_id) JOIN cases c ON c.case_id=j.case_id "
                       "WHERE g.grant_id=?", (action["grant_id"],)).fetchone()
    if (not row or row["worker_uid"] != action["peer_uid"] or row["revoked_at"] is not None or row["job_state"] != "running"
            or row["case_state"] not in EXECUTABLE_CASE_STATES
            or row["attempt_no"] != row["job_attempt"] or row["lifecycle_round"] != row["job_round"]
            or row["lifecycle_round"] != row["case_round"] or row["input_digest"] != row["job_input"]
            or row["lease_owner"] != row["job_owner"]):
        raise ValueError("remote task authority changed")
    now = datetime.now(UTC)
    created = datetime.fromisoformat(row["created_at"])
    if created.tzinfo is None or created > now:
        raise ValueError("remote grant not yet valid")
    for name in ("expires_at", "lease_expires_at"):
        expiry = datetime.fromisoformat(row[name])
        if expiry.tzinfo is None or expiry <= now:
            raise ValueError("remote task lease expired")
    validate_running(conn, grant_id=action["grant_id"], contract=contract)
    from .broker_input import project
    from .project_investigation_source import require_current

    inputs = project(conn, job_id=row["job_id"], case_id=row["case_id"],
                     lifecycle_round=row["lifecycle_round"], input_digest=row["input_digest"],
                     request_id=action["request_id"])
    if "investigation_source" in inputs:
        for name, source in inputs.get('investigation_sources', {inputs['repos'][0]: inputs['investigation_source']}).items():
            require_current(config, name, source)

    from .project_verifier_job import validate_job

    validate_job(conn, inputs)
    plan = json.loads(action["plan_json"])
    if "investigation_source" in inputs:
        from .project_investigation_checkout import validate_plan

        validate_plan(config, inputs, plan, job_id=row["job_id"], case_id=row["case_id"], request_id=action["request_id"], conn=conn)
    from .project_verification_runs import validate_dispatch
    verification_timeout = validate_dispatch(conn, action)
    from .project_verification_workspace import guard as workspace_guard

    workspace_timeout = workspace_guard(conn, action)
    if workspace_timeout is not None:
        verification_timeout = workspace_timeout
    if verification_timeout is not None:
        plan["verification_timeout_seconds"] = verification_timeout
    directory = receipt_directory(config.raw["runtime"])
    if (type(plan.get("guard_version")) is not int or plan["guard_version"] != (2 if directory else 1)
            or plan["contract_digest"] != contract.fingerprint or plan["input_digest"] != row["input_digest"]
            or not matches(config, plan)):
        raise ValueError("remote plan configuration changed")
    if directory and (plan.get("receipt_directory") != directory
                      or not isinstance(plan.get("command_digest"), str) or len(plan["command_digest"]) != 64
                      or any(c not in "0123456789abcdef" for c in plan["command_digest"])):
        raise ValueError("remote receipt binding changed")
    return plan


def run_one(conn, config, *, contract_reader, transport=run_process, stop_event=None):
    with transaction(conn):
        if stop_event is not None and stop_event.is_set():
            return {"state": "stopped"}
        if conn.execute("SELECT 1 FROM broker_remote_actions a LEFT JOIN broker_remote_results r USING(request_id) "
                        f"WHERE {UNSETTLED} LIMIT 1").fetchone():
            return {"state": "occupied"}
        action = conn.execute("SELECT a.* FROM broker_remote_actions a WHERE a.state='queued' AND NOT EXISTS ("
                              "SELECT 1 FROM project_verification_workspaces w JOIN broker_remote_actions p "
                              "ON p.request_id=w.preparation_request_id WHERE w.request_id=a.request_id "
                              "AND p.state IN ('queued','running')) ORDER BY a.created_at,a.request_id LIMIT 1").fetchone()
        if action is None:
            return {"state": "idle"}
        try:
            plan = _live(conn, config, action, contract_reader())
        except (ValueError, TypeError, KeyError, OSError):
            conn.execute("UPDATE broker_remote_actions SET state='cancelled',updated_at=? WHERE request_id=?",
                         (iso_now(), action["request_id"]))
            return {"state": "cancelled"}
        conn.execute("UPDATE broker_remote_actions SET state='running',updated_at=? WHERE request_id=?", (iso_now(), action["request_id"]))

    def heartbeat():
        if stop_event is not None and stop_event.is_set():
            raise ValueError("remote consumer stopping")
        with transaction(conn):
            _live(conn, config, action, contract_reader())

    state = "unknown"
    try:
        from .project_investigation_observation import observe as observe_baseline

        baseline = observe_baseline(conn, config, action, transport=transport, heartbeat=heartbeat)
        if baseline not in (None, "matched"):
            state = "cancelled"
            return {"state": state, "request_id": action["request_id"], "reason": "investigation_baseline_unverified", "remote_cleanup_verified": False}
        from .project_verification_sources import observe as observe_sources
        before = observe_sources(conn, config, action, phase="before", transport=transport, heartbeat=heartbeat)
        if before not in (None, "matched"):
            state = "cancelled"
            return {"state": state, "request_id": action["request_id"], "reason": "source_preflight_failed", "remote_cleanup_verified": False}
        heartbeat()
        result = transport(argv=plan_argv(plan, plan["command"]), cwd="/", stdin=b"", heartbeat=heartbeat,
                           env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                           timeout=min(plan.get("verification_timeout_seconds", 7200), 7200), heartbeat_interval=1, output_limit=128000, detailed=True, keepalive=True)
        checkout_after = None
        if isinstance(result, dict) and type(result.get("exit_code")) is int and 0 <= result["exit_code"] < 255 and result["exit_code"] not in (124, 125):
            observe_sources(conn, config, action, phase="after", transport=transport, heartbeat=heartbeat)
            if plan.get("investigation_checkout") and isinstance(result.get("stdout"),str) and result["stdout"].startswith("K3_CHECKOUT_V1:"):
                from .project_investigation_after import collect

                checkout_after = collect(plan, transport=transport, heartbeat=heartbeat)

        with transaction(conn):
            _live(conn, config, action, contract_reader())
            if (not isinstance(result, dict) or type(result.get("exit_code")) is not int or not -255 <= result["exit_code"] <= 255
                    or any(not isinstance(result.get(key), str) for key in ("stdout", "stderr"))
                    or len((result["stdout"] + result["stderr"]).encode()) > 128000):
                raise ValueError("invalid remote output")
            from .project_investigation_checkout import record as record_checkout

            result = record_checkout(conn, plan, result)
            if checkout_after is not None:
                from .project_investigation_after import record as record_after

                record_after(conn, plan, checkout_after)
            # SSH transport failure does not establish a remote command exit.
            next_state = "unknown" if result["exit_code"] in (124, 125, 255) or result["exit_code"] < 0 else ("succeeded" if result["exit_code"] == 0 else "failed")
            conn.execute("INSERT INTO broker_remote_results VALUES(?,?,?,?,?)",
                         (action["request_id"], result["exit_code"], result["stdout"], result["stderr"], iso_now()))
            conn.execute("UPDATE broker_remote_actions SET state=?,updated_at=? WHERE request_id=?",
                         (next_state, iso_now(), action["request_id"]))
        state = next_state
    finally:
        with transaction(conn):
            # Cancelling a running SSH client does not prove its remote child stopped.
            conn.execute("UPDATE broker_remote_actions SET state=?,updated_at=? WHERE request_id=? AND state IN ('running','cancelled')",
                         (state, iso_now(), action["request_id"]))
    return {"state": state, "request_id": action["request_id"], "remote_cleanup_verified": False}
