"""Queue grant-bound sandbox plans without network I/O in the broker listener."""

import hashlib
from copy import deepcopy

from .broker_budget import validate_running
from .broker_grants import verify_bound_task
from .broker_input import project
from .broker_policy import execution_allowed
from .broker_protocol import decode_request
from .broker_remote_state import UNSETTLED
from .codex_remote import build_remote_command
from .config import Config
from .db import transaction
from .execution_transport import target
from .ids import canonical_json, digest
from .remote_guard import receipt_directory, wrap
from .timeutil import iso_now


def read(conn, config, request, *, peer_uid, contract_reader, now=None):
    """Read bounded output pages; never enqueue, renew or repeat execution."""
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "remote_read":
        raise ValueError("remote read required")
    contract = contract_reader() if contract_reader else None
    with transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            raise ValueError("remote execution disabled")
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        validate_running(conn, grant_id=binding["grant_id"], contract=contract)
        row = conn.execute("SELECT a.state,r.exit_code,r.stdout,r.stderr FROM broker_remote_actions a "
                           "LEFT JOIN broker_remote_results r USING(request_id) "
                           "WHERE a.request_id=? AND a.grant_id=? AND a.peer_uid=?",
                           (request["params"]["remote_request_id"], binding["grant_id"], peer_uid)).fetchone()
        if row is None:
            raise ValueError("remote result unavailable")
        offset = request["params"]["offset"]
        stdout, stderr = row["stdout"] or "", row["stderr"] or ""
        if offset > max(len(stdout), len(stderr)):
            raise ValueError("remote offset outside output")
        end = offset + 4096
        from .project_investigation_after import for_request

        checkout_after = for_request(conn, request["params"]["remote_request_id"])
        return {"remote_request_id": request["params"]["remote_request_id"], "state": row["state"],
                "exit_code": row["exit_code"], "stdout": stdout[offset:end], "stderr": stderr[offset:end],
                "next_offset": end if end < max(len(stdout), len(stderr)) else None,
                "remote_cleanup_verified": False,
                **({"checkout_after": checkout_after} if checkout_after is not None else {})}


def submit(conn, config, request, *, peer_uid, contract_reader, now=None):
    request = decode_request(canonical_json(request).encode())
    if request["method"] != "remote_submit":
        raise ValueError("remote submission required")
    contract = contract_reader() if contract_reader else None
    with transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            raise ValueError("remote execution disabled")
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        validate_running(conn, grant_id=binding["grant_id"], contract=contract)
        previous = conn.execute("SELECT * FROM broker_remote_actions WHERE request_id=?", (request["request_id"],)).fetchone()
        fingerprint = digest(request)
        if previous:
            if (previous["peer_uid"] != peer_uid or previous["grant_id"] != binding["grant_id"]
                    or previous["request_digest"] != fingerprint):
                raise ValueError("remote request identity changed")
            return {"accepted": True, "request_id": request["request_id"], "state": previous["state"]}
        if conn.execute("SELECT 1 FROM broker_remote_actions a LEFT JOIN broker_remote_results r USING(request_id) "
                        f"WHERE a.grant_id=? AND (a.state='queued' OR {UNSETTLED}) LIMIT 1",
                        (binding["grant_id"],)).fetchone():
            raise ValueError("remote operation unresolved")
        inputs = project(conn, job_id=binding["job_id"], case_id=request["params"]["case_id"],
                         lifecycle_round=binding["lifecycle_round"], input_digest=binding["input_digest"],
                         request_id=request["request_id"])
        if "investigation_source" in inputs:
            from .project_investigation_source import require_current

            for name, source in inputs.get('investigation_sources', {inputs['repos'][0]: inputs['investigation_source']}).items():
                require_current(config, name, source)
        from .project_verifier_job import require_remote

        require_remote(conn, inputs, request)
        remote = request["params"]["remote"]
        allowed = inputs["repos"]
        if remote["mode"] == "work" and remote["repo"] not in allowed:
            raise ValueError("repository outside task scope")
        raw = deepcopy(config.raw)
        raw["repositories"] = {name: raw["repositories"][name] for name in allowed if name in raw["repositories"]}
        if len(raw["repositories"]) != len(set(allowed)):
            raise ValueError("task repository unavailable")
        command, checkout = remote["command"], None
        if "investigation_source" in inputs:
            from .project_investigation_checkout import prepare

            command, checkout = prepare(conn, config, inputs, job_id=binding["job_id"],
                                        case_id=request["params"]["case_id"], request_id=request["request_id"], remote=remote)
        from .project_investigation_checkout import companion_seeds

        plan = build_remote_command(Config(raw, config.path), case_id=request["params"]["case_id"],
                                    mode=remote["mode"], repo_name=remote["repo"], command=command,
                                    # Generic Case jobs need the same per-job writable
                                    # fence as Bug investigations. A later tool run in
                                    # the same Case must not inherit an earlier clone.
                                    work_id=binding["job_id"] if remote["mode"] == "work" else None,
                                    seed_work_id=checkout.get("seed_job_id") if checkout else None,
                                    seed_work_ids=companion_seeds(checkout) if checkout else None)
        directory = receipt_directory(config.raw["runtime"])
        journal = {"receipt_directory": directory, "request_id": request["request_id"]} if directory else {}
        stored = {"command": wrap(plan, **journal), "guard_version": 2 if directory else 1,
                  "input_digest": binding["input_digest"], **target(config), "contract_digest": contract.fingerprint}
        if checkout is not None:
            stored["investigation_checkout"] = checkout
        from .project_verification_runs import bind_submission
        verification_run_id = bind_submission(
            conn, request_id=request["request_id"], grant_id=binding["grant_id"], remote=remote,
        )
        if verification_run_id:
            stored["verification_run_id"] = verification_run_id
        if directory:
            stored.update(receipt_directory=directory, command_digest=hashlib.sha256(plan.encode()).hexdigest())
        stamp = iso_now()
        conn.execute("INSERT INTO broker_remote_actions VALUES(?,?,?,?,?,'queued',?,?)",
                     (request["request_id"], peer_uid, binding["grant_id"], fingerprint,
                      canonical_json(stored), stamp, stamp))
        from .project_verification_workspace import enqueue as enqueue_workspace

        action = conn.execute("SELECT * FROM broker_remote_actions WHERE request_id=?", (request["request_id"],)).fetchone()
        enqueue_workspace(conn, Config(raw, config.path), inputs=inputs, action=action, case_id=request["params"]["case_id"])
        return {"accepted": True, "request_id": request["request_id"], "state": "queued"}
