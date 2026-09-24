"""Bind an explicitly requested coding job to one current Bug investigation."""

import json

from . import coding_tasks
from .project_bugs import BugConflict, _bug, _event, _revision, _text

CONTEXT = {"bug_id", "round_id", "expected_revision", "actor", "source"}
FIELDS = (coding_tasks.FIELDS - {"case_id"}) | {
    "bug_id", "round_id", "expected_revision", "source"
}
CONTINUATION_FIELDS = FIELDS | {"predecessor_job_id"}


def require_predecessor(conn, context):
    """An ordering fence, not a claim that the predecessor repaired anything."""
    predecessor = context["predecessor_job_id"]
    _text(predecessor, "predecessor job")
    row = conn.execute(
        "SELECT j.state FROM project_investigation_jobs i JOIN jobs j USING(job_id) "
        "WHERE i.round_id=? AND i.job_id=?", (context["round_id"], predecessor),
    ).fetchone()
    if row is None or row["state"] not in {"succeeded", "failed", "cancelled"}:
        raise BugConflict("predecessor must be a settled job in this investigation")
    from .project_round_readiness import require

    require(conn, context["round_id"], job_id=predecessor)


def validate(conn, context, case_id, *, config=None):
    if not isinstance(context, dict) or set(context) not in (CONTEXT, CONTEXT | {"verification"}, CONTEXT | {"predecessor_job_id"}):
        raise ValueError("invalid investigation binding")
    from .project_investigation_source import validate as validate_source

    validate_source(context["source"])
    for key in ("bug_id", "round_id", "actor"):
        _text(context[key], key)
    bug = _bug(conn, context["bug_id"])
    _revision(bug, context["expected_revision"])
    if bug["case_id"] != case_id:
        raise BugConflict("investigation belongs to a different Case")
    from .store import EXECUTABLE_CASE_STATES

    case = conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if case["state"] not in {*EXECUTABLE_CASE_STATES, "intake"}:
        raise BugConflict("Case must be explicitly resumed before investigation launch")
    round_ = conn.execute(
        "SELECT * FROM project_bug_rounds WHERE round_id=? AND bug_id=?",
        (context["round_id"], context["bug_id"]),
    ).fetchone()
    if round_ is None or round_["archived_at"] is not None:
        raise BugConflict("investigation is missing or archived")
    from .project_close_lifecycle import require_current as require_lifecycle
    from .project_field_writer_config import validate as validate_writer

    writer = config.raw.get("project_integration", {}).get("field_writer") if config else None
    closing_ids = validate_writer(writer)["closing_status_ids"] if writer is not None else ()
    require_lifecycle(conn, bug_id=context["bug_id"], round_id=context["round_id"],
                      closing_status_ids=closing_ids)
    if "verification" in context:
        from .project_round_readiness import require
        from .project_verifier_job import plan_binding

        if round_["execution_state"] not in {"planned","succeeded","failed","cancelled"}:
            raise BugConflict("settle investigation before launching independent verification")
        require(conn, context["round_id"])
        plan_binding(conn, context["verification"], round_id=context["round_id"], source=context["source"])
    elif "predecessor_job_id" in context:
        from .project_round_readiness import require

        require_predecessor(conn, context)
        if round_["execution_state"] not in {"succeeded", "failed", "cancelled"}:
            raise BugConflict("settle the investigation before continuing")
        require(conn, context["round_id"])
        if conn.execute("SELECT 1 FROM project_verification_plans WHERE round_id=?",
                        (context["round_id"],)).fetchone():
            raise BugConflict("start a new round to preserve the existing verification plan")
    elif round_["execution_state"] not in {"planned", "running"}:
        raise BugConflict("investigation requires explicit resumption or a new round")
    from .project_verification_runs import unsettled

    if unsettled(conn, context["round_id"]):
        raise BugConflict("verification execution remains unsettled")
    if conn.execute(
        "SELECT 1 FROM project_bug_operations WHERE bug_id=? AND state IN ('prepared','dispatched','unknown')",
        (context["bug_id"],),
    ).fetchone():
        raise BugConflict("reconcile pending Bug writes before coding")
    return bug


def submit(conn, config, payload, *, request_origin=None):
    if not isinstance(payload, dict) or set(payload) not in (FIELDS, CONTINUATION_FIELDS):
        raise ValueError("investigation task requires exact fields")
    bug = _bug(conn, payload["bug_id"])
    context = {key: payload[key] for key in CONTEXT - {"actor"}}
    context["actor"] = config.control_operator_id
    if "predecessor_job_id" in payload:
        context["predecessor_job_id"] = payload["predecessor_job_id"]
    request = {key: payload[key] for key in coding_tasks.FIELDS - {"case_id"}}
    request["case_id"] = bug["case_id"]
    return coding_tasks.submit(conn, config, request, project_context=context, request_origin=request_origin)


def record(conn, context, job_id):
    """Called only inside the job/input insertion transaction."""
    from .store import transition_case

    bug = _bug(conn, context["bug_id"])
    case = conn.execute("SELECT state,version FROM cases WHERE case_id=?", (bug["case_id"],)).fetchone()
    if case["state"] == "intake":
        transition_case(conn, case_id=bug["case_id"], after="triage", actor_type="operator",
                        actor_id=context["actor"], reason="Operator requested a Bug coding investigation",
                        expected_version=case["version"], idempotency_key="bug-launch:"+job_id)
    conn.execute(
        "INSERT INTO project_investigation_jobs(job_id,round_id) VALUES(?,?)",
        (job_id, context["round_id"]),
    )
    conn.execute(
        "UPDATE project_bugs SET revision=revision+1 WHERE bug_id=?",
        (context["bug_id"],),
    )
    _event(conn, context["bug_id"], context["actor"], "coding_job_created",
           {"job_id": job_id, **({"predecessor_job_id": context["predecessor_job_id"]}
                                if "predecessor_job_id" in context else {})}, context["round_id"])


def projection(conn, bug_id):
    rows = [dict(row) for row in conn.execute(
        """SELECT j.job_id,b.round_id,j.state,j.error_class,j.created_at,j.updated_at,
           json_extract(j.context_json,'$.agent') AS agent,
           json_extract(j.context_json,'$.repositories') AS repositories_json,
           json_extract(j.context_json,'$.project_investigation.source') AS source_json,
           CASE WHEN json_type(j.context_json,'$.project_investigation.verification')='object'
                THEN 'verification' ELSE 'investigation' END AS purpose,
           json_extract(j.context_json,'$.project_investigation.predecessor_job_id') AS predecessor_job_id
           FROM project_investigation_jobs b JOIN jobs j USING(job_id)
           JOIN project_bug_rounds r USING(round_id) WHERE r.bug_id=?
           ORDER BY j.created_at DESC,j.job_id DESC LIMIT 100""", (bug_id,)
    )]
    for row in rows:
        row["repositories"] = json.loads(row["repositories_json"]) if row["repositories_json"] else []
        source = row.pop("source_json")
        row["source"] = json.loads(source) if source else None
        from .project_investigation_observation import projection as source_observations

        row["source_observations"] = source_observations(conn, row["job_id"])
        from .project_investigation_checkout import projection as checkout_observations

        row["checkout_observations"] = checkout_observations(conn, row["job_id"])
        from .project_investigation_after import projection as checkout_after

        row["checkout_after_observations"] = checkout_after(conn, row["job_id"])
        row["source_observation"] = "observed" if row["checkout_observations"] else "not_collected"
    return rows
