"""Pre-bind verification intent to a broker action, without granting execution.

Only control orchestration calls prepare. Workers cannot create or change these
rows. The existing broker still authenticates, scopes, leases and executes every
command. A real exit receipt proves execution, not source identity or the oracle.
"""

import json
import re
from uuid import UUID

from .broker_remote_state import UNSETTLED
from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _event, _text
from .timeutil import iso_now, parse_iso, utc_now


def _context(conn, plan_id, step_id, grant_id):
    plan = conn.execute(
        "SELECT p.*,r.bug_id,r.archived_at,r.execution_state,b.case_id "
        "FROM project_verification_plans p JOIN project_bug_rounds r USING(round_id) "
        "JOIN project_bugs b USING(bug_id) WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise ValueError("verification plan unavailable")
    latest = conn.execute(
        "SELECT max(version) FROM project_verification_plans WHERE round_id=?",
        (plan["round_id"],),
    ).fetchone()[0]
    if (
        plan["version"] != latest
        or plan["archived_at"]
        or plan["execution_state"] not in {"planned", "running"}
    ):
        raise BugConflict("verification plan no longer executable")
    definition = json.loads(plan["plan_json"])
    step = next((s for s in definition["steps"] if s["id"] == step_id), None)
    if step is None or step["layer"] not in {"static", "build", "software_test", "device_function"}:
        raise ValueError("remote command requires a software or device verification step")
    if step["depends_on"]:
        from .project_verification_reviews import assessments

        states = {s["step_id"]:s["state"] for s in assessments(conn, plan_id)}
        if any(states.get(key) != "passed" for key in step["depends_on"]):
            raise BugConflict("verification dependencies require independently verified results")
    grant = conn.execute(
        "SELECT g.*,j.case_id,j.state AS job_state,j.attempt_no AS job_attempt,"
        "j.lifecycle_round AS job_round,j.input_digest AS job_input,j.lease_owner AS job_owner,"
        "j.lease_expires_at,c.lifecycle_round AS case_round "
        "FROM broker_grants g JOIN jobs j USING(job_id) JOIN cases c ON c.case_id=j.case_id "
        "WHERE grant_id=?",
        (grant_id,),
    ).fetchone()
    if (
        grant is None
        or grant["case_id"] != plan["case_id"]
        or grant["revoked_at"]
        or grant["job_state"] != "running"
        or grant["attempt_no"] != grant["job_attempt"]
        or grant["lifecycle_round"] != grant["job_round"]
        or grant["job_round"] != grant["case_round"]
        or grant["input_digest"] != grant["job_input"]
        or grant["lease_owner"] != grant["job_owner"]
        or parse_iso(grant["created_at"]) > utc_now()
        or any(
            parse_iso(grant[k]) <= utc_now() for k in ("expires_at", "lease_expires_at")
        )
    ):
        raise BugConflict("verification execution binding is not live")
    return plan, definition, step, grant


def prepare(
    conn,
    *,
    plan_id,
    step_id,
    grant_id,
    remote_request_id,
    remote,
    actor,
    request_id,
    source_paths=None,
    config=None,
):
    """Control-only intent; call before submitting the exact remote request.

    No output/result or verified flag is accepted. Existing executor authority is
    mandatory but this intent does not extend it or dispatch work.
    """
    for name, value in (("actor", actor), ("request", request_id), ("step", step_id)):
        _text(value, name)
    if (
        not isinstance(remote_request_id, str)
        or str(UUID(remote_request_id)) != remote_request_id
    ):
        raise ValueError("remote request requires a canonical UUID")
    if (
        not isinstance(remote, dict)
        or set(remote) != {"mode", "repo", "command"}
        or remote["mode"] != "work"
    ):
        raise ValueError("verification requires an exact repository command")
    _text(remote["repo"], "repository")
    _text(remote["command"], "command", 16000)
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", remote["repo"])
        or "\x00" in remote["command"]
        or re.search(r"[\ud800-\udfff]", remote["command"])
        or len(remote["command"].encode()) > 32768
    ):
        raise ValueError("verification command does not fit the broker protocol")
    signature = digest(
        {
            "plan_id": plan_id,
            "step_id": step_id,
            "grant_id": grant_id,
            "remote_request_id": remote_request_id,
            "remote": remote,
            **({"source_paths": source_paths} if source_paths is not None else {}),
        }
    )
    with transaction(conn):
        old = conn.execute(
            "SELECT * FROM project_verification_runs WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("verification request reused for different content")
            return dict(old)
        plan, definition, step, _ = _context(conn, plan_id, step_id, grant_id)
        if unsettled(conn, plan["round_id"]):
            raise BugConflict(
                "settle prior verification execution before preparing another run"
            )
        repositories = {
            r["repository"]
            for r in definition["repositories"]
            if r["id"] in step["repositories"]
        }
        if remote["repo"] not in repositories:
            raise ValueError("verification repository differs from plan")
        if conn.execute(
            "SELECT 1 FROM broker_remote_actions WHERE request_id=?",
            (remote_request_id,),
        ).fetchone():
            raise BugConflict("verification cannot adopt an already submitted action")
        if conn.execute(
            "SELECT 1 FROM project_verification_runs WHERE remote_request_id=?",
            (remote_request_id,),
        ).fetchone():
            raise BugConflict("remote action already belongs to a verification run")
        run_id = new_id("pvr")
        conn.execute(
            "INSERT INTO project_verification_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                plan_id,
                step_id,
                actor,
                request_id,
                signature,
                grant_id,
                remote_request_id,
                canonical_json(remote),
                iso_now(),
            ),
        )
        if source_paths is not None:
            from .project_verification_sources import freeze

            freeze(
                conn,
                config,
                run_id=run_id,
                plan=plan,
                definition=definition,
                step=step,
                source_paths=source_paths,
            )
        from .project_verification_reviews import freeze_dependencies

        freeze_dependencies(conn, run_id, plan_id, step)
        _event(
            conn,
            plan["bug_id"],
            actor,
            "verification_run_prepared",
            {"run_id": run_id, "plan_id": plan_id, "step_id": step_id},
            plan["round_id"],
        )
        return dict(
            conn.execute(
                "SELECT * FROM project_verification_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        )


def unsettled(conn, round_id, *, job_id=None):
    return (
        conn.execute(
            "SELECT 1 FROM project_verification_runs v JOIN project_verification_plans p USING(plan_id) "
            "JOIN broker_remote_actions a ON a.request_id=v.remote_request_id "
            "LEFT JOIN broker_remote_results r ON r.request_id=a.request_id "
            f"WHERE p.round_id=? AND (? IS NULL OR v.grant_id IN (SELECT grant_id FROM broker_grants WHERE job_id=?)) AND (a.state='queued' OR {UNSETTLED}) LIMIT 1",
            (round_id,job_id,job_id),
        ).fetchone()
        is not None
    )


def bind_submission(conn, *, request_id, grant_id, remote):
    """Called inside the authenticated broker submission transaction."""
    if not conn.in_transaction:
        raise ValueError("broker transaction required")
    run = conn.execute(
        "SELECT * FROM project_verification_runs WHERE remote_request_id=?",
        (request_id,),
    ).fetchone()
    if run is None:
        return None
    if run["grant_id"] != grant_id or json.loads(run["remote_json"]) != remote:
        raise BugConflict("verification command differs from its prepared intent")
    _context(conn, run["plan_id"], run["step_id"], grant_id)
    from .project_verification_reviews import require_dependencies

    require_dependencies(conn, run)
    return run["run_id"]


def projection(conn, plan_id, *, run_id=None):
    """Read broker-owned outcomes, never worker manifests. No pass inference."""
    runs = []
    for run in conn.execute(
        "SELECT * FROM project_verification_runs WHERE plan_id=? AND (? IS NULL OR run_id=?) ORDER BY rowid",
        (plan_id, run_id, run_id),
    ):
        action = conn.execute(
            "SELECT * FROM broker_remote_actions WHERE request_id=?",
            (run["remote_request_id"],),
        ).fetchone()
        result = conn.execute(
            "SELECT * FROM broker_remote_results WHERE request_id=?",
            (run["remote_request_id"],),
        ).fetchone()
        bound = bool(
            action
            and action["grant_id"] == run["grant_id"]
            and json.loads(action["plan_json"]).get("verification_run_id")
            == run["run_id"]
        )
        state = action["state"] if bound else ("unknown" if action else "prepared")
        receipt = None
        if bound and result:
            code = result["exit_code"]
            if (
                state not in {"succeeded", "failed"}
                or code in (124, 125, 255)
                or code < 0
                or (state == "succeeded") != (code == 0)
            ):
                state = "unknown"
            else:
                receipt = {
                    "request_id": run["remote_request_id"],
                    "exit_code": code,
                    "finished_at": result["finished_at"],
                    "output_digest": digest(
                        {"stdout": result["stdout"], "stderr": result["stderr"]}
                    ),
                }
        elif state in {"succeeded", "failed"}:
            state = "unknown"
        runs.append(
            {
                "run_id": run["run_id"],
                "step_id": run["step_id"],
                "execution_state": state,
                "receipt": receipt,
                "verification_state": "not_run" if state == "prepared" else "unknown",
                "bindings_verified": False,
                "oracle_verified": False,
            }
        )
        from .project_verification_sources import projection as source_projection

        runs[-1]["source_observations"] = source_projection(conn, run["run_id"])
        from .project_verification_workspace import projection as workspace_projection

        runs[-1]["workspace_preparation"] = workspace_projection(conn, run["remote_request_id"])
    return runs


def validate_dispatch(conn, action):
    """Recheck current plan and investigation on every execution heartbeat."""
    run = conn.execute(
        "SELECT * FROM project_verification_runs WHERE remote_request_id=?",
        (action["request_id"],),
    ).fetchone()
    if run is None and not json.loads(action["plan_json"]).get("verification_run_id"):
        return
    if (
        not run
        or run["grant_id"] != action["grant_id"]
        or json.loads(action["plan_json"]).get("verification_run_id") != run["run_id"]
    ):
        raise BugConflict("verification dispatch binding changed")
    _, _, step, _ = _context(conn, run["plan_id"], run["step_id"], run["grant_id"])
    from .project_verification_reviews import require_dependencies

    require_dependencies(conn, run)
    return step["timeout_seconds"]


def read_for_worker(conn, config, request, *, peer_uid, contract_reader, now=None):
    """One bounded page under the worker's current authenticated task binding.

    Keep immutable task input unchanged. Workers can refresh this read-only inbox
    while running; returned intents do not confer execution or Project authority.
    """
    from .broker_budget import validate_running
    from .broker_grants import verify_bound_task
    from .broker_input import project
    from .broker_policy import execution_allowed
    from .broker_protocol import decode_request, encode_response

    request = decode_request(canonical_json(request).encode())
    if request["method"] != "verification_list":
        raise ValueError("verification list required")
    contract = contract_reader() if contract_reader else None
    with transaction(conn):
        if not execution_allowed(conn, config, contract=contract):
            raise ValueError("remote execution disabled")
        binding = verify_bound_task(conn, request["params"], peer_uid=peer_uid, now=now)
        validate_running(conn, grant_id=binding["grant_id"], contract=contract)
        inputs = project(
            conn,
            job_id=binding["job_id"],
            case_id=request["params"]["case_id"],
            lifecycle_round=binding["lifecycle_round"],
            input_digest=binding["input_digest"],
            request_id=request["request_id"],
        )
        rows = conn.execute(
            "SELECT * FROM project_verification_runs WHERE grant_id=? AND run_id>? ORDER BY run_id LIMIT 2",
            (binding["grant_id"], request["params"]["after_id"]),
        ).fetchall()
        items = []
        for row in rows[:1]:
            view = projection(conn, row["plan_id"], run_id=row["run_id"])[0]
            dispatchable = False
            try:
                plan, _, _, _ = _context(
                    conn, row["plan_id"], row["step_id"], row["grant_id"]
                )
                from .project_verification_reviews import require_dependencies

                require_dependencies(conn, row)
                from .project_verifier_job import require_remote

                require_remote(conn, inputs, {"request_id": row["remote_request_id"], "params": {"remote": json.loads(row["remote_json"])}})
                dispatchable = (
                    view["execution_state"] == "prepared"
                    and json.loads(row["remote_json"])["repo"] in inputs["repos"]
                    and not unsettled(conn, plan["round_id"])
                )
            except ValueError:
                pass  # Old/held plans remain visible for result reconciliation.
            items.append(
                {
                    "run_id": row["run_id"],
                    "plan_id": row["plan_id"],
                    "step_id": row["step_id"],
                    "remote_request_id": row["remote_request_id"],
                    "remote": json.loads(row["remote_json"]),
                    "execution_state": view["execution_state"],
                    "verification_state": view["verification_state"],
                    "dispatchable": dispatchable,
                }
            )
        result = {
            "items": items,
            "next_cursor": rows[0]["run_id"] if len(rows) > 1 else None,
        }
        encode_response(request_id=request["request_id"], result=result)
        return result
