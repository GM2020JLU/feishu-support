"""Operator oracle decisions bound to exact control-owned execution evidence.

Source sampling is necessary, never sufficient, for a pass. The operator explicitly
attests the procedure, environment and oracle; workers cannot call this surface.
Device execution remains available, but command output is not independent proof
of device identity or loaded artifacts. Device passes require a trusted collector
that is not yet integrated; operator attestation cannot substitute for it.
"""

import json
from contextlib import nullcontext

from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _event, _text
from .project_verification_runs import projection
from .timeutil import iso_now

FIELDS = {"run_id", "request_id", "evidence_digest", "expected_review_id",
          "verdict", "rationale", "attested"}
PAGE_SIZE = 8000


def _dependencies(steps, ids):
    return [{"step_id":s["step_id"], "run_id":s["run_id"], "evidence_digest":s["evidence_digest"],
             "review_id":s["review"]["review_id"] if s["review"] else None}
            for s in steps if s["step_id"] in ids]


def freeze_dependencies(conn, run_id, plan_id, step):
    if step["depends_on"]:
        binding = _dependencies(assessments(conn, plan_id), step["depends_on"])
        conn.execute("INSERT INTO project_verification_dependency_bindings VALUES(?,?)",
                     (run_id, canonical_json(binding)))


def require_dependencies(conn, run):
    definition = json.loads(conn.execute("SELECT plan_json FROM project_verification_plans WHERE plan_id=?", (run["plan_id"],)).fetchone()[0])
    step = next(s for s in definition["steps"] if s["id"] == run["step_id"])
    if not step["depends_on"]:
        return
    view = next((s for s in assessments(conn, run["plan_id"]) if s["run_id"] == run["run_id"]), None)
    if view is None or any(key in view["pass_blockers"] for key in ("dependency_changed", "dependency_not_passed")):
        raise BugConflict("verification dependency evidence changed; prepare a fresh run")


def _device_evidence_blockers(conn, definition, step, run):
    """A printed identity/hash proves only that the command printed that text.

    Keep device runs executable and reviewable as inconclusive/failed, but do
    not promote their stdout/stderr to trusted device or loaded-image evidence.
    A future collector must bind observations to this run, node, device lease
    and actual artifact; until then even an exact echo cannot clear this gate.
    """
    blockers = ["device_evidence_unavailable"]
    if step["artifacts"]:
        blockers.append("artifact_evidence_unavailable")
    return blockers


def assessments(conn, plan_id):
    plan = conn.execute(
        """SELECT p.*,r.bug_id,r.archived_at FROM project_verification_plans p
           JOIN project_bug_rounds r USING(round_id) WHERE plan_id=?""", (plan_id,),
    ).fetchone()
    if plan is None:
        raise ValueError("verification plan unavailable")
    latest = conn.execute("SELECT max(version) FROM project_verification_plans WHERE round_id=?",
                          (plan["round_id"],)).fetchone()[0]
    definition = json.loads(plan["plan_json"])
    runs = {run["step_id"]:run for run in projection(conn, plan_id)}
    steps = []
    for step in definition["steps"]:
        run = runs.get(step["id"])
        dependencies = [s for s in steps if s["step_id"] in step["depends_on"]]
        item = {"step_id":step["id"], "run_id":run["run_id"] if run else None,
                "state":"not_run", "review":None, "evidence_digest":None,
                "pass_blockers":[], "review_available":False}
        steps.append(item)
        if run is None:
            continue
        binding = conn.execute(
            """SELECT v.remote_json,g.job_id,g.attempt_no,g.lifecycle_round,g.input_digest,
               j.attempt_no AS current_attempt,j.lifecycle_round AS current_round,
               j.input_digest AS current_input,c.lifecycle_round AS case_round
               FROM project_verification_runs v JOIN broker_grants g USING(grant_id)
               JOIN jobs j USING(job_id) JOIN cases c ON c.case_id=j.case_id WHERE run_id=?""",
            (run["run_id"],),
        ).fetchone()
        source = conn.execute("SELECT * FROM project_verification_sources WHERE run_id=?",
                              (run["run_id"],)).fetchone()
        deps = [{key:d[key] for key in ("step_id", "run_id", "state", "review")} for d in dependencies]
        frozen = conn.execute("SELECT binding_json FROM project_verification_dependency_bindings WHERE run_id=?",
                              (run["run_id"],)).fetchone()
        item["evidence_digest"] = digest({
            "plan_digest":plan["plan_digest"], "plan_current":latest == plan["version"],
            "archived":plan["archived_at"], "step":step, "execution":run,
            "binding":dict(binding) if binding else None,
            "source_binding":dict(source) if source else None, "dependencies":deps,
            "dependency_binding":frozen[0] if frozen else None,
        })
        current = latest == plan["version"] and plan["archived_at"] is None
        bound = binding and (binding["attempt_no"] == binding["current_attempt"]
                            and binding["lifecycle_round"] == binding["current_round"] == binding["case_round"]
                            and binding["input_digest"] == binding["current_input"])
        settled = run["execution_state"] not in {"prepared", "queued", "running"}
        item["review_available"] = bool(current and bound and settled)
        blockers = item["pass_blockers"]
        if not current or not bound:
            blockers.append("obsolete_binding")
        if run["execution_state"] != "succeeded" or not run["receipt"]:
            blockers.append("execution_not_successful")
        observations = run["source_observations"]
        if (source is None or len(observations) != 2
                or {o["phase"] for o in observations} != {"before", "after"}
                or any(o["state"] != "matched" or not o["sources"] for o in observations)):
            blockers.append("source_not_verified")
        if step["layer"] == "device_function":
            blockers.extend(_device_evidence_blockers(conn, definition, step, run))
        else:
            if step["devices"] or step["layer"] not in {"static", "build", "software_test"}:
                blockers.append("device_evidence_unavailable")
            if step["artifacts"]:
                blockers.append("artifact_evidence_unavailable")
        if any(d["state"] != "passed" for d in dependencies):
            blockers.append("dependency_not_passed")
        if step["depends_on"] and (frozen is None or json.loads(frozen[0]) != _dependencies(dependencies, step["depends_on"])):
            blockers.append("dependency_changed")
        review = conn.execute("SELECT * FROM project_verification_reviews WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                              (run["run_id"],)).fetchone()
        item["state"] = "not_run" if run["execution_state"] == "prepared" else "running" if not settled else "unknown"
        if review:
            item["review"] = {key:review[key] for key in ("review_id", "actor", "verdict", "rationale", "created_at")}
            if review["evidence_digest"] != item["evidence_digest"]:
                item["state"] = "stale"
            else:
                item["state"] = review["verdict"]
                if item["state"] == "passed" and blockers:
                    item["state"] = "stale"
    return steps


def summarize(conn, plan_id):
    steps = assessments(conn, plan_id)
    definition = json.loads(conn.execute("SELECT plan_json FROM project_verification_plans WHERE plan_id=?", (plan_id,)).fetchone()[0])
    required = {s["id"] for s in definition["steps"] if s["required"]}
    states = {s["state"] for s in steps if s["step_id"] in required}
    functional = any(s["required"] and s["layer"] in {"software_test", "device_function", "stability"} for s in definition["steps"])
    state = ("failed" if "failed" in states else "passed" if states == {"passed"} and functional
             else "running" if "running" in states else "not_run" if states == {"not_run"} else "unknown")
    return {"steps":steps, "verification_state":state, "functional_step_required":functional,
            "verdict_source":"operator_review", "closure_authorized":False}


def _run(conn, run_id):
    row = conn.execute("SELECT * FROM project_verification_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("verification run unavailable")
    return row


def detail(conn, run_id):
    _text(run_id, "run ID")
    with nullcontext() if conn.in_transaction else transaction(conn, immediate=False):
        run = _run(conn, run_id)
        assessment = next((s for s in assessments(conn, run["plan_id"]) if s["run_id"] == run_id), None)
        if assessment is None:
            raise BugConflict("a newer run supersedes this verification")
        plan = conn.execute("SELECT plan_json FROM project_verification_plans WHERE plan_id=?", (run["plan_id"],)).fetchone()
        definition = json.loads(plan[0])
        step = next(s for s in definition["steps"] if s["id"] == run["step_id"])
        return assessment | {"plan_id":run["plan_id"], "step":step,
                             "bindings":{kind:[value for value in definition[kind] if value["id"] in step[kind]]
                                         for kind in ("repositories", "artifacts", "devices")},
                             "remote":json.loads(run["remote_json"]),
                             "execution":projection(conn, run["plan_id"], run_id=run_id)[0]}


def output(conn, *, run_id, evidence_digest, channel, offset):
    if not isinstance(channel, str) or channel not in {"stdout", "stderr"} or type(offset) is not int or offset < 0:
        raise ValueError("invalid verification output page")
    with nullcontext() if conn.in_transaction else transaction(conn, immediate=False):
        view = detail(conn, run_id)
        if view["evidence_digest"] != evidence_digest:
            raise BugConflict("verification evidence changed")
        run = _run(conn, run_id)
        row = conn.execute("SELECT stdout,stderr FROM broker_remote_results WHERE request_id=?", (run["remote_request_id"],)).fetchone()
        if row is None or view["execution"]["receipt"] is None:
            raise ValueError("no settled execution output available")
        text = row[channel]
        if offset > len(text):
            raise ValueError("output offset beyond evidence")
        end = min(offset + PAGE_SIZE, len(text))
        return {"text":text[offset:end], "offset":offset, "next_offset":end if end < len(text) else None,
                "length":len(text), "channel":channel, "evidence_digest":evidence_digest}


def record(conn, *, actor, payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("verification review requires exact fields")
    _text(actor, "actor")
    for key in ("run_id", "request_id", "evidence_digest", "rationale"):
        _text(payload[key], key, 4000 if key == "rationale" else 256)
    if (not isinstance(payload["verdict"], str) or payload["verdict"] not in {"passed", "failed", "inconclusive"}
            or payload["attested"] is not True
            or payload["expected_review_id"] is not None and not isinstance(payload["expected_review_id"], str)):
        raise ValueError("explicit operator attestation and a valid verdict required")
    signature = digest(payload)
    with transaction(conn):
        old = conn.execute("SELECT * FROM project_verification_reviews WHERE actor=? AND request_id=?",
                           (actor, payload["request_id"])).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("review request reused for different content")
            return dict(old)
        view = detail(conn, payload["run_id"])
        previous = view["review"]["review_id"] if view["review"] else None
        if (view["evidence_digest"] != payload["evidence_digest"]
                or previous != payload["expected_review_id"] or not view["review_available"]):
            raise BugConflict("verification evidence or review changed; refresh before deciding")
        if payload["verdict"] == "passed" and view["pass_blockers"]:
            raise BugConflict("verification pass requires complete bound evidence")
        if payload["verdict"] == "failed" and view["execution"]["receipt"] is None:
            raise BugConflict("unknown execution can only be recorded inconclusive")
        review_id = new_id("pvw")
        conn.execute("INSERT INTO project_verification_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                     (review_id, payload["run_id"], actor, payload["request_id"], signature,
                      payload["evidence_digest"], payload["verdict"], payload["rationale"], iso_now()))
        plan = conn.execute("SELECT r.bug_id,r.round_id FROM project_verification_plans p JOIN project_bug_rounds r USING(round_id) WHERE p.plan_id=?", (view["plan_id"],)).fetchone()
        conn.execute("UPDATE project_bugs SET revision=revision+1 WHERE bug_id=?", (plan["bug_id"],))
        _event(conn, plan["bug_id"], actor, "verification_reviewed", {"review_id":review_id, "run_id":payload["run_id"], "verdict":payload["verdict"]}, plan["round_id"])
        return dict(conn.execute("SELECT * FROM project_verification_reviews WHERE review_id=?", (review_id,)).fetchone())
