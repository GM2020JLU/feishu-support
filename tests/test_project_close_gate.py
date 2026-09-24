# ruff: noqa: F811 -- shared isolated fixtures
"""Closure gate state machine and its consumption inside dispatch; no live Project."""

import sqlite3
from datetime import timedelta

import pytest
from test_project_bug_operations import setup  # noqa: F401

from k3_support import project_bug_operations as operations
from k3_support import project_close_gate as close_gate
from k3_support.project_bug_controls import execute
from k3_support.project_bugs import BugConflict
from k3_support.timeutil import utc_now

CHANGE = {"transition_id": "to-close", "target_status_id": "closed"}


def expiry(hours=1):
    return (utc_now() + timedelta(hours=hours)).isoformat()


@pytest.fixture
def passed(monkeypatch):
    state = {
        "plan_id": "plan-1",
        "round_id": "round-1",
        "verification_state": "passed",
        "digest": "e" * 64,
    }
    monkeypatch.setattr(close_gate, "evidence", lambda conn, bug_id: dict(state))
    return state


def request(conn, setup, request_id="close-1", expires_at=None):
    _, req, _ = setup
    return close_gate.request(
        conn,
        actor="owner",
        request_id=request_id,
        bug_id=req["bug_id"],
        change=dict(CHANGE),
        expires_at=expires_at or expiry(),
    )


def decide(conn, approval, *, approve=True, request_id="decision-1", digest=None):
    return close_gate.decide(
        conn,
        approval_id=approval["approval_id"],
        actor="owner",
        request_id=request_id,
        approve=approve,
        expected_digest=digest or approval["action_digest"],
    )


def prepare_close(conn, setup):
    cfg, req, adapter = setup
    op = operations.prepare(conn, **req | {"action": "bug.close", "change": dict(CHANGE)})
    return cfg, adapter, op


def approval_row(conn, approval_id):
    return conn.execute(
        "SELECT * FROM project_close_approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()


def test_request_requires_round_and_passed_verification(conn, setup, monkeypatch):
    with pytest.raises(BugConflict, match="investigation round"):
        request(conn, setup)
    state = {"plan_id": "p", "round_id": "r", "verification_state": "running", "digest": "e" * 64}
    monkeypatch.setattr(close_gate, "evidence", lambda c, b: dict(state))
    with pytest.raises(BugConflict, match="passed verification"):
        request(conn, setup)
    state["verification_state"] = "passed"
    approval = request(conn, setup)
    assert approval["status"] == "requested"
    assert approval["verification_digest"] == "e" * 64


def test_request_replay_is_idempotent_and_one_live_approval_per_bug(conn, setup, passed):
    deadline = expiry()
    first = request(conn, setup, expires_at=deadline)
    assert request(conn, setup, expires_at=deadline)["approval_id"] == first["approval_id"]
    with pytest.raises(BugConflict, match="different content"):
        request(conn, setup, expires_at=expiry(2))
    with pytest.raises(BugConflict, match="live close approval"):
        request(conn, setup, request_id="close-2")


def test_fresh_passed_evidence_supersedes_old_approval_without_auto_approving(conn, setup, passed):
    first = request(conn, setup)
    decide(conn, first)
    passed["digest"] = "f" * 64
    second = request(conn, setup, request_id="fresh-evidence")
    old = approval_row(conn, first["approval_id"])
    assert old["status"] == "revoked" and old["decided_by"] == "owner"
    assert second["status"] == "requested"
    assert conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='close_approval_superseded'").fetchone()[0] == 1


def test_decision_binds_exact_content_and_replays_only_identically(conn, setup, passed):
    approval = request(conn, setup)
    with pytest.raises(BugConflict, match="content changed"):
        decide(conn, approval, digest="0" * 64)
    decided = decide(conn, approval)
    assert decided["status"] == "approved" and decided["decided_by"] == "owner"
    assert decide(conn, approval)["decided_at"] == decided["decided_at"]
    with pytest.raises(BugConflict, match="different decision"):
        decide(conn, approval, approve=False)
    with pytest.raises(BugConflict, match="is approved"):
        decide(conn, approval, request_id="decision-2")


def test_denied_approval_is_terminal_and_frees_the_live_slot(conn, setup, passed):
    approval = request(conn, setup)
    assert decide(conn, approval, approve=False)["status"] == "denied"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_close_approvals SET status='approved' WHERE approval_id=?",
            (approval["approval_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM project_close_approvals")
    assert request(conn, setup, request_id="close-2")["status"] == "requested"


def test_dispatch_consumes_a_single_use_approval_bound_to_the_operation(conn, setup, passed):
    cfg, adapter, op = prepare_close(conn, setup)
    with pytest.raises(PermissionError, match="approved close approval"):
        operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    assert adapter.writes == []
    approval = request(conn, setup)
    with pytest.raises(PermissionError, match="approved close approval"):
        operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    decide(conn, approval)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=adapter
    )
    assert result["state"] == "confirmed"
    assert adapter.writes[0]["closure_approval_id"] == approval["approval_id"]
    row = approval_row(conn, approval["approval_id"])
    assert row["status"] == "consumed"
    assert row["consumed_operation_id"] == op["operation_id"]
    assert request(conn, setup, request_id="close-2")["status"] == "requested"


def test_changed_verification_evidence_invalidates_an_approved_closure(conn, setup, passed):
    cfg, adapter, op = prepare_close(conn, setup)
    approval = request(conn, setup)
    decide(conn, approval)
    passed["digest"] = "f" * 64
    with pytest.raises(PermissionError, match="evidence changed"):
        operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    assert adapter.writes == []
    assert approval_row(conn, approval["approval_id"])["status"] == "approved"


def test_expired_approval_never_consumes_and_is_swept_on_next_request(
    conn, setup, passed, monkeypatch
):
    cfg, adapter, op = prepare_close(conn, setup)
    approval = request(conn, setup)
    decide(conn, approval)
    future = (utc_now() + timedelta(hours=2)).isoformat()
    monkeypatch.setattr(close_gate, "iso_now", lambda: future)
    with pytest.raises(PermissionError, match="approved close approval"):
        operations.dispatch(conn, cfg, operation_id=op["operation_id"], transport=adapter)
    assert adapter.writes == []
    fresh = request(conn, setup, request_id="close-2", expires_at=expiry(3))
    assert fresh["status"] == "requested"
    assert approval_row(conn, approval["approval_id"])["status"] == "expired"


def test_controls_expose_the_close_approval_flow(conn, setup, passed):
    cfg, req, _ = setup
    approval = execute(
        conn,
        cfg,
        action="request-close-approval",
        payload={
            "bug_id": req["bug_id"],
            "request_id": "close-1",
            "change": dict(CHANGE),
            "expires_at": expiry(),
        },
    )
    decided = execute(
        conn,
        cfg,
        action="decide-close-approval",
        payload={
            "approval_id": approval["approval_id"],
            "request_id": "decision-1",
            "approve": True,
            "expected_digest": approval["action_digest"],
        },
    )
    assert decided["status"] == "approved"
    listing = execute(
        conn, cfg, action="close-approvals", payload={"bug_id": req["bug_id"]}
    )
    assert listing["items"][0]["approval_id"] == approval["approval_id"]


def test_evidence_runs_against_the_real_schema(conn, setup):
    """No mocked evidence: the closure queries must work on migrated tables."""
    from k3_support import project_bugs as bugs
    from k3_support import project_verification as verification

    _, req, _ = setup
    bug_id = req["bug_id"]

    with pytest.raises(BugConflict, match="active investigation round"):
        close_gate.evidence(conn, bug_id)
    bugs.start_round(conn, bug_id=bug_id, actor="owner", request_id="round-1",
                     reason="synthetic", expected_revision=2)
    with pytest.raises(BugConflict, match="verification plan"):
        close_gate.evidence(conn, bug_id)
    round_id = conn.execute("SELECT round_id FROM project_bug_rounds").fetchone()[0]
    verification.publish(conn, bug_id=bug_id, round_id=round_id, actor="owner",
                         request_id="plan-1", expected_revision=3, plan={
        "title": "synthetic", "artifacts": [], "devices": [],
        "repositories": [{"id": "r", "node": "n", "repository": "repo", "branch": "b",
                          "base_commit": "a" * 40, "candidate_commit": "b" * 40}],
        "steps": [{"id": "s", "title": "t", "layer": "software_test", "required": True,
                    "depends_on": [], "repositories": ["r"], "artifacts": [], "devices": [],
                    "node": "n", "environment": "e", "procedure": "p", "oracle": "o",
                    "timeout_seconds": 60}]})
    state = close_gate.evidence(conn, bug_id)
    assert state["verification_state"] != "passed"
    with pytest.raises(BugConflict, match="passed verification"):
        close_gate.request(conn, actor="owner", request_id="close-real", bug_id=bug_id,
                           change=dict(CHANGE), expires_at=expiry())
