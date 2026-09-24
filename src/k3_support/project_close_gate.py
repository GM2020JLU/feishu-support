"""Bug closure gate: reviewed repair, passed verification and single-use approval.

Closure is authorized by an explicit human approval bound to the exact close
action and to the verification evidence observed at request time. Consumption
happens inside the dispatch transaction; an unknown or rejected write never
resurrects a consumed approval, and changed verification evidence invalidates an
approved closure before any write. Coding rounds also require a current ready
repair review. The existing verification_digest column binds both evidence
sources for these rounds; verification-only rounds retain their original digest.
This module never reads the network.
"""

import json
from contextlib import nullcontext

from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _bug, _event, _text
from .project_verification_reviews import summarize
from .timeutil import iso_now, parse_iso, utc_now


def evidence(conn, bug_id):
    round_row = conn.execute(
        "SELECT round_id FROM project_bug_rounds WHERE bug_id=? AND archived_at IS NULL ORDER BY rowid DESC LIMIT 1",
        (bug_id,),
    ).fetchone()
    if round_row is None:
        raise BugConflict("closure requires an active investigation round")
    from .project_close_lifecycle import require_current

    require_current(conn, bug_id=bug_id, round_id=round_row["round_id"])
    plan = conn.execute(
        "SELECT plan_id FROM project_verification_plans WHERE round_id=? ORDER BY version DESC LIMIT 1",
        (round_row["round_id"],),
    ).fetchone()
    if plan is None:
        raise BugConflict("closure requires a verification plan")
    summary = summarize(conn, plan["plan_id"])
    fingerprint = digest(
        {
            "plan_id": plan["plan_id"],
            "verification_state": summary["verification_state"],
            "steps": [
                {
                    "step_id": step["step_id"],
                    "run_id": step["run_id"],
                    "state": step["state"],
                    "evidence_digest": step["evidence_digest"],
                }
                for step in summary["steps"]
            ],
        }
    )
    return {
        "plan_id": plan["plan_id"],
        "round_id": round_row["round_id"],
        "verification_state": summary["verification_state"],
        "digest": fingerprint,
    }


def action(bug, change):
    return {
        "kind": "project_bug_close",
        "bug_id": bug["bug_id"],
        "host": bug["host"],
        "project_key": bug["project_key"],
        "type_key": bug["type_key"],
        "item_id": bug["item_id"],
        "transition_id": change["transition_id"],
        "target_status_id": change["target_status_id"],
    }


def _expire(conn, bug_id, now):
    conn.execute(
        "UPDATE project_close_approvals SET status='expired',updated_at=? WHERE bug_id=? AND status IN ('requested','approved') AND expires_at<=?",
        (now, bug_id, now),
    )


def projection(row):
    return {
        key: row[key]
        for key in (
            "approval_id",
            "bug_id",
            "actor",
            "action_digest",
            "verification_digest",
            "status",
            "requested_at",
            "expires_at",
            "decided_at",
            "decided_by",
            "consumed_at",
            "consumed_operation_id",
        )
    }


def _current_evidence(conn, bug_id, config):
    state = evidence(conn, bug_id)
    # Verification-only jobs do not claim a code repair. Once this round has
    # coding investigation work, its independently reviewed changeset is part
    # of the close decision, not merely an informational UI label.
    coding = False
    for row in conn.execute(
        "SELECT j.input_digest,b.payload_json FROM project_investigation_jobs i "
        "JOIN jobs j USING(job_id) LEFT JOIN broker_inputs b USING(job_id) "
        "WHERE i.round_id=?", (state["round_id"],)
    ):
        try:
            payload = json.loads(row["payload_json"])
            context = payload["context_extra"]["project_investigation"]
            if (digest(payload) != row["input_digest"]
                    or context["bug_id"] != bug_id
                    or context["round_id"] != state["round_id"]):
                raise ValueError("investigation binding changed")
            coding = coding or "verification" not in context
        except (TypeError, ValueError, KeyError):
            raise BugConflict("closure repair scope is unavailable") from None
    if coding:
        from .project_repair_reviews import detail as repair_detail

        repair = repair_detail(conn, config, bug_id=bug_id, round_id=state["round_id"])
        if (repair["review_state"] != "current" or repair["repair_state"] != "ready"
                or not repair["can_mark_ready"]):
            raise BugConflict("closure requires a current ready repair review")
        state["repair_review_id"] = repair["review"]["review_id"]
        state["repair_evidence_digest"] = repair["evidence_digest"]
        state["digest"] = digest({
            "verification": state["digest"],
            "repair_review_id": state["repair_review_id"],
            "repair_evidence_digest": state["repair_evidence_digest"],
        })
    if config is not None:
        from .project_close_lifecycle import require_current
        from .project_field_writer_config import validate

        writer = config.raw.get("project_integration", {}).get("field_writer")
        if writer is not None:
            require_current(conn, bug_id=bug_id, round_id=state["round_id"],
                            closing_status_ids=validate(writer)["closing_status_ids"])
    return state


def detail(conn, approval_id, *, config=None):
    """Read-only, exact action/evidence preview shared by authenticated channels."""
    _text(approval_id, "close approval")
    with nullcontext() if conn.in_transaction else transaction(conn, immediate=False):
        row = conn.execute("SELECT * FROM project_close_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise ValueError("close approval does not exist")
        intent = json.loads(row["action_json"])
        if digest(intent) != row["action_digest"]:
            raise BugConflict("close approval content binding is invalid")
        result = projection(row)
        result["action"] = intent
        result["expired"] = row["expires_at"] <= iso_now()
        blockers = []
        if row["status"] != "requested":
            blockers.append("approval_not_requested")
        if result["expired"]:
            blockers.append("approval_expired")
        try:
            current = _current_evidence(conn, row["bug_id"], config)
            result["current_verification"] = current
            if current["verification_state"] != "passed" or current["digest"] != row["verification_digest"]:
                blockers.append("verification_changed")
        except BugConflict:
            result["current_verification"] = None
            blockers.append("verification_unavailable")
        result["approval_blockers"] = blockers
        result["can_approve"] = not blockers
        result["can_deny"] = row["status"] == "requested" and not result["expired"]
        return result


def request(conn, *, actor, request_id, bug_id, change, expires_at, config=None):
    from . import project_bug_operations as operations

    _text(actor, "actor")
    _text(request_id, "close approval request", 256)
    operations._change("bug.close", change)
    if parse_iso(expires_at) <= utc_now():
        raise ValueError("close approval expiry must be in the future")
    with transaction(conn):
        bug = _bug(conn, bug_id)
        intent = action(bug, change)
        old = conn.execute(
            "SELECT * FROM project_close_approvals WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if (
                old["action_digest"] != digest(intent)
                or old["expires_at"] != expires_at
            ):
                raise BugConflict(
                    "close approval request ID reused for different content"
                )
            return projection(old)
        state = _current_evidence(conn, bug_id, config)
        if state["verification_state"] != "passed":
            raise BugConflict("closure approval requires a passed verification")
        now = iso_now()
        _expire(conn, bug_id, now)
        # A fresh passed plan can replace unusable old-round approvals without
        # erasing their decision or implicitly approving the new intent.
        stale = conn.execute(
            "SELECT approval_id FROM project_close_approvals WHERE bug_id=? "
            "AND status IN ('requested','approved') AND verification_digest!=?",
            (bug_id, state["digest"]),
        ).fetchall()
        for prior in stale:
            conn.execute("UPDATE project_close_approvals SET status='revoked',updated_at=? WHERE approval_id=?",
                         (now, prior["approval_id"]))
            _event(conn, bug_id, actor, "close_approval_superseded",
                   {"approval_id": prior["approval_id"], "reason": "verification_changed"})
        if conn.execute(
            "SELECT 1 FROM project_close_approvals WHERE bug_id=? AND status IN ('requested','approved')",
            (bug_id,),
        ).fetchone():
            raise BugConflict("bug already has a live close approval")
        approval_id = new_id("pca")
        conn.execute(
            """INSERT INTO project_close_approvals(approval_id,bug_id,actor,request_id,action_json,
                   action_digest,verification_digest,status,requested_at,expires_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'requested',?,?,?,?)""",
            (
                approval_id,
                bug_id,
                actor,
                request_id,
                canonical_json(intent),
                digest(intent),
                state["digest"],
                now,
                expires_at,
                now,
                now,
            ),
        )
        _event(
            conn,
            bug_id,
            actor,
            "close_approval_requested",
            {"approval_id": approval_id, "plan_id": state["plan_id"]},
        )
        return projection(
            conn.execute(
                "SELECT * FROM project_close_approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        )


def decide(conn, *, approval_id, actor, request_id, approve, expected_digest, config=None):
    _text(actor, "actor")
    _text(request_id, "close decision request", 256)
    if type(approve) is not bool:
        raise ValueError("explicit close decision required")
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM project_close_approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ValueError("close approval does not exist")
        wanted = "approved" if approve else "denied"
        if conn.execute(
            "SELECT 1 FROM project_close_approvals WHERE decided_by=? AND decision_request_id=? AND approval_id!=?",
            (actor, request_id, approval_id),
        ).fetchone():
            raise BugConflict("close decision request reused for a different approval")
        if row["action_digest"] != expected_digest:
            raise BugConflict("close approval content changed; refresh before deciding")
        if row["decision_request_id"] == request_id and row["decided_by"] == actor:
            if (row["status"] != "denied") != approve:
                raise BugConflict(
                    "close decision request reused for a different decision"
                )
            return projection(row)
        now = iso_now()
        if row["status"] != "requested":
            raise BugConflict(f"close approval is {row['status']}")
        if row["expires_at"] <= now:
            _expire(conn, row["bug_id"], now)
            raise BugConflict("close approval expired")
        if approve:
            current = _current_evidence(conn, row["bug_id"], config)
            if current["verification_state"] != "passed" or current["digest"] != row["verification_digest"]:
                raise BugConflict("verification evidence changed; request a new close approval")
        updated = conn.execute(
            """UPDATE project_close_approvals SET status=?,decided_at=?,decided_by=?,
                   decision_request_id=?,updated_at=? WHERE approval_id=? AND status='requested'""",
            (wanted, now, actor, request_id, now, approval_id),
        )
        if updated.rowcount != 1:
            raise BugConflict("close approval changed during decision")
        _event(
            conn,
            row["bug_id"],
            actor,
            "close_approval_decided",
            {"approval_id": approval_id, "approved": approve},
        )
        return projection(
            conn.execute(
                "SELECT * FROM project_close_approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        )


def consume(conn, *, bug, change, operation_id, config=None):
    """Single-use gate inside the dispatch transaction; caller owns the transaction."""
    expected = digest(action(bug, change))
    now = iso_now()
    row = conn.execute(
        "SELECT * FROM project_close_approvals WHERE bug_id=? AND status='approved' ORDER BY rowid DESC LIMIT 1",
        (bug["bug_id"],),
    ).fetchone()
    if row is None or row["expires_at"] <= now or row["action_digest"] != expected:
        raise PermissionError("closure requires a current approved close approval")
    state = _current_evidence(conn, bug["bug_id"], config)
    if (
        state["verification_state"] != "passed"
        or state["digest"] != row["verification_digest"]
    ):
        raise PermissionError("verification evidence changed since the close approval")
    updated = conn.execute(
        """UPDATE project_close_approvals SET status='consumed',consumed_at=?,consumed_operation_id=?,updated_at=?
           WHERE approval_id=? AND status='approved' AND expires_at>?""",
        (now, operation_id, now, row["approval_id"], now),
    )
    if updated.rowcount != 1:
        raise PermissionError("close approval changed during dispatch")
    return row["approval_id"]


def status_for_bug(conn, bug_id, *, config=None):
    with nullcontext() if conn.in_transaction else transaction(conn, immediate=False):
        rows = conn.execute(
            "SELECT approval_id FROM project_close_approvals WHERE bug_id=? ORDER BY rowid DESC LIMIT 10",
            (bug_id,),
        ).fetchall()
        return [detail(conn, row["approval_id"], config=config) for row in rows]
