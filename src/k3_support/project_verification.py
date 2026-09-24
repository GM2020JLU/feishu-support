"""Versioned verification intent, not evidence or authority to execute.

Only authenticated control callers may publish plans. Candidate hashes and
environment descriptions are expectations; a future trusted collector must
independently observe them before any step can pass.
"""

import json
import re

from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _bug, _event, _revision, _text
from .timeutil import iso_now

LAYERS = {
    "static",
    "build",
    "software_test",
    "ram_boot",
    "persistent_flash",
    "device_function",
    "stability",
}
DEVICE_LAYERS = {"ram_boot", "persistent_flash", "device_function", "stability"}


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("verification plan requires exact object fields")


def _items(value, label, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= 100:
        raise ValueError(f"invalid {label} list")


def _hash(value, lengths):
    if (
        not isinstance(value, str)
        or len(value) not in lengths
        or not re.fullmatch(r"[0-9a-f]+", value)
    ):
        raise ValueError("expected a full lowercase content hash")


def validate(plan):
    _object(plan, {"title", "repositories", "artifacts", "devices", "steps"})
    _text(plan["title"], "plan title", 500)
    json.dumps(plan, allow_nan=False)
    if len(canonical_json(plan).encode()) > 128 * 1024:
        raise ValueError("verification plan is too large")
    catalogs = {}
    for kind, keys in (
        (
            "repositories",
            {"id", "node", "repository", "branch", "base_commit", "candidate_commit"},
        ),
        ("artifacts", {"id", "sha256"}),
        ("devices", {"id", "node", "identity"}),
    ):
        _items(plan[kind], kind, 1 if kind == "repositories" else 0)
        ids = set()
        for item in plan[kind]:
            _object(item, keys)
            for key, value in item.items():
                _text(value, key, 1000)
            if item["id"] in ids:
                raise ValueError("duplicate verification binding")
            ids.add(item["id"])
            if kind == "repositories":
                _hash(item["base_commit"], {40, 64})
                _hash(item["candidate_commit"], {40, 64})
            if kind == "artifacts":
                _hash(item["sha256"], {64})
        catalogs[kind] = ids
    _items(plan["steps"], "steps", 1)
    seen = set()
    required = set()
    for step in plan["steps"]:
        _object(
            step,
            {
                "id",
                "title",
                "layer",
                "required",
                "depends_on",
                "repositories",
                "artifacts",
                "devices",
                "node",
                "environment",
                "procedure",
                "oracle",
                "timeout_seconds",
            },
        )
        for key in ("id", "title", "node", "environment", "procedure", "oracle"):
            _text(step[key], key, 4000)
        if step["id"] in seen:
            raise ValueError("duplicate verification step")
        if not isinstance(step["layer"], str) or step["layer"] not in LAYERS:
            raise ValueError("invalid verification layer")
        if type(step["required"]) is not bool:
            raise ValueError("required must be boolean")
        if (
            type(step["timeout_seconds"]) is not int
            or not 1 <= step["timeout_seconds"] <= 604800
        ):
            raise ValueError("invalid verification timeout")
        for key in ("depends_on", "repositories", "artifacts", "devices"):
            _items(step[key], key, 1 if key == "repositories" else 0)
            for value in step[key]:
                _text(value, key)
            values = set(step[key])
            if len(values) != len(step[key]) or not values <= (
                seen if key == "depends_on" else catalogs[key]
            ):
                raise ValueError("invalid or forward verification dependency")
        if step["required"] and not set(step["depends_on"]) <= required:
            raise ValueError("required steps cannot depend on optional steps")
        if step["layer"] in DEVICE_LAYERS and (
            not step["devices"] or not step["artifacts"]
        ):
            raise ValueError(
                "device verification requires device and artifact bindings"
            )
        seen.add(step["id"])
        if step["required"]:
            required.add(step["id"])
    if not required:
        raise ValueError("verification requires at least one mandatory step")


def _projection(row, conn):
    from .project_verification_runs import projection

    result = {
        "plan_id": row["plan_id"],
        "round_id": row["round_id"],
        "version": row["version"],
        "plan_digest": row["plan_digest"],
        "created_at": row["created_at"],
        "definition": json.loads(row["plan_json"]),
        "verification_state": "not_run",
        "bindings_verified": False,
        "execution_available": False,
    }
    result["runs"] = projection(conn, row["plan_id"])
    states = {run["execution_state"] for run in result["runs"]}
    if states:
        result["verification_state"] = (
            "running"
            if states <= {"queued", "running", "prepared"} and states != {"prepared"}
            else "not_run"
            if states == {"prepared"}
            else "unknown"
        )
    from .project_verification_reviews import summarize

    result["operator_verification"] = summarize(conn, row["plan_id"])
    if any(step["review"] for step in result["operator_verification"]["steps"]):
        result["verification_state"] = result["operator_verification"]["verification_state"]
    return result


def current(conn, round_id):
    row = conn.execute(
        "SELECT * FROM project_verification_plans WHERE round_id=? ORDER BY version DESC LIMIT 1",
        (round_id,),
    ).fetchone()
    if row is None:
        return None
    return _projection(row, conn)


def publish(conn, *, bug_id, round_id, actor, request_id, expected_revision, plan):
    """Save a plan version; does not approve commands, hardware or Bug closure."""
    for key, value in (
        ("actor", actor),
        ("request ID", request_id),
        ("round", round_id),
    ):
        _text(value, key)
    validate(plan)
    signature = digest(
        {"bug_id": bug_id, "plan": plan, "expected_revision": expected_revision}
    )
    with transaction(conn):
        bug = _bug(conn, bug_id)
        round_ = conn.execute(
            "SELECT * FROM project_bug_rounds WHERE round_id=? AND bug_id=?",
            (round_id, bug_id),
        ).fetchone()
        if round_ is None:
            raise ValueError("verification requires a matching investigation round")
        old = conn.execute(
            "SELECT * FROM project_verification_plans WHERE round_id=? AND actor=? AND request_id=?",
            (round_id, actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("request ID reused for different content")
            return _projection(old, conn)
        _revision(bug, expected_revision)
        if round_["archived_at"] or round_["execution_state"] not in {
            "planned",
            "paused",
            "human",
            "blocked",
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise BugConflict("settle the investigation before changing its plan")
        if conn.execute(
            "SELECT 1 FROM project_bug_operations WHERE bug_id=? AND state IN ('prepared','dispatched','unknown')",
            (bug_id,),
        ).fetchone():
            raise BugConflict("settle the pending write before changing its plan")
        prior = current(conn, round_id)
        from .project_verification_runs import unsettled

        if unsettled(conn, round_id):
            raise BugConflict("settle verification execution before replacing its plan")
        from .project_round_readiness import require

        require(conn, round_id)
        plan_id = new_id("pvp")
        conn.execute(
            "INSERT INTO project_verification_plans VALUES(?,?,?,?,?,?,?,?,?)",
            (
                plan_id,
                round_id,
                prior["version"] + 1 if prior else 1,
                actor,
                request_id,
                signature,
                digest(plan),
                canonical_json(plan),
                iso_now(),
            ),
        )
        conn.execute(
            "UPDATE project_bugs SET revision=revision+1 WHERE bug_id=?", (bug_id,)
        )
        conn.execute(
            "UPDATE project_bug_rounds SET verification_state='not_run' WHERE round_id=?",
            (round_id,),
        )
        _event(
            conn,
            bug_id,
            actor,
            "verification_plan_published",
            {"plan_id": plan_id, "plan_digest": digest(plan)},
            round_id,
        )
        return current(conn, round_id)
