"""Real closure aggregation over synthetic execution/Project observations."""
# ruff: noqa: F811 -- isolated execution fixture
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_project_chat_approvals import setup
from test_project_verification_runs import context  # noqa: F401

from k3_support import project_bug_controls as controls
from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_bugs as bugs
from k3_support import project_close_gate as gate
from k3_support.project_transport import ProjectReceipt
from k3_support.timeutil import iso_now, utc_now


def observe(conn, bug, status, key):
    return bugs.observe(conn, bug_id=bug["bug_id"], observation_id=key,
        expected_sequence=bugs._bug(conn, bug["bug_id"])["snapshot_sequence"], observed_at=iso_now(),
        payload={"fields": {"description": key}, "status_id": status,
                 "closure": {"closed": None, "reason": None}, "remote_version": None, "schema_digest": None})


def writer(cfg):
    cfg.raw.setdefault("project_integration", {})["field_writer"] = {
        "enabled": True, "user_key": "fixture-user", "allowed_fields": ["description"],
        "risk_policy": "forbid", "closing_status_ids": ["CLOSED"]}


def request(conn, cfg, bug, key="close"):
    return controls.execute(conn, cfg, action="request-close-approval", payload={
        "bug_id": bug["bug_id"], "request_id": key,
        "change": {"transition_id": "close-choice", "target_status_id": "CLOSED"},
        "expires_at": (utc_now() + timedelta(hours=1)).isoformat()})


@pytest.mark.parametrize("approved", [False, True])
def test_observed_reopen_blocks_old_approval_decision_and_consumption(conn, context, approved):
    cfg, bug, _ = setup(conn, context)
    writer(cfg)
    observe(conn, bug, "OPEN", "initial")
    approval = request(conn, cfg, bug)
    if approved:
        gate.decide(conn, config=cfg, approval_id=approval["approval_id"], actor=cfg.control_operator_id,
                    request_id="approve", approve=True, expected_digest=approval["action_digest"])
    reviews = [tuple(r) for r in conn.execute("SELECT * FROM project_verification_reviews")]
    observe(conn, bug, "CLOSED", "colleague-closed")
    observe(conn, bug, "OPEN", "colleague-reopened")
    detail = controls.execute(conn, cfg, action="close-approval-detail", payload={"approval_id": approval["approval_id"]})
    assert not detail["can_approve"] and "verification_unavailable" in detail["approval_blockers"]
    with pytest.raises(bugs.BugConflict, match="reopened"):
        request(conn, cfg, bug, "new-close-using-old-proof")
    if approved:
        with pytest.raises(bugs.BugConflict, match="reopened"):
            gate.consume(conn, config=cfg, bug=bug, operation_id="must-not-dispatch",
                         change={"transition_id": "close-choice", "target_status_id": "CLOSED"})
    else:
        with pytest.raises(bugs.BugConflict, match="reopened"):
            controls.execute(conn, cfg, action="decide-close-approval", payload={
                "approval_id": approval["approval_id"], "request_id": "late-approval",
                "approve": True, "expected_digest": approval["action_digest"]})
    assert [tuple(r) for r in conn.execute("SELECT * FROM project_verification_reviews")] == reviews
    assert conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 0


def test_same_timestamp_events_and_new_round_do_not_reuse_previous_verification(conn, context, monkeypatch):
    monkeypatch.setattr(bugs, "iso_now", lambda: "2025-01-01T00:00:00.000+00:00")
    cfg, bug, _ = setup(conn, context)
    writer(cfg)
    observe(conn, bug, "CLOSED", "closed")
    observe(conn, bug, "OPEN", "reopened")
    with pytest.raises(bugs.BugConflict, match="reopened"):
        request(conn, cfg, bug)
    conn.execute("UPDATE project_bug_rounds SET execution_state='succeeded' WHERE bug_id=?", (bug["bug_id"],))
    fresh = bugs.start_round(conn, bug_id=bug["bug_id"], actor=cfg.control_operator_id,
        request_id="new-round", reason="Investigate recurrence",
        expected_revision=bugs._bug(conn, bug["bug_id"])["revision"])
    assert fresh["verification_state"] == "not_run"
    with pytest.raises(bugs.BugConflict, match="verification plan"):
        request(conn, cfg, bug, "new-round-close")
    # All newly generated lifecycle events share a timestamp; their order still works.
    assert conn.execute("SELECT count(*) FROM project_bug_events WHERE kind='observed' AND created_at=?",
                        ("2025-01-01T00:00:00.000+00:00",)).fetchone()[0] == 2


def test_ordinary_progress_and_field_observations_do_not_invalidate_pass(conn, context):
    cfg, bug, _ = setup(conn, context)
    writer(cfg)
    before = gate.evidence(conn, bug["bug_id"])["digest"]
    observe(conn, bug, "OPEN", "initial")
    observe(conn, bug, "IN_TEST", "ordinary-progress")
    observe(conn, bug, "IN_TEST", "changed-description")
    assert request(conn, cfg, bug)["verification_digest"] == before


def test_confirmed_own_close_requires_new_round_without_rewriting_history(conn, context):
    cfg, bug, _ = setup(conn, context)
    snapshot = observe(conn, bug, "OPEN", "initial")
    grant = grants.issue(conn, actor=cfg.control_operator_id, request_id="scope",
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(), scope={
            "host": bug["host"], "project_key": bug["project_key"], "type_key": bug["type_key"],
            "bug_ids": [bug["bug_id"]], "actions": ["bug.close"], "fields": [],
            "transitions": ["close-choice"], "repositories": [], "devices": []})
    operation = operations.prepare(conn, bug_id=bug["bug_id"], snapshot_id=snapshot["snapshot_id"],
        actor=cfg.control_operator_id, request_id="close-op", grant_id=grant["grant_id"],
        expected_revision=bugs._bug(conn, bug["bug_id"])["revision"], action="bug.close",
        change={"transition_id": "close-choice", "target_status_id": "CLOSED"})
    # The provider/dispatch receipt is synthetic; real reconciliation records its event.
    conn.execute("UPDATE project_bug_operations SET state='dispatched',write_json=?,write_digest=? WHERE operation_id=?",
        (json.dumps({"operation_id": operation["operation_id"]}), "d" * 64, operation["operation_id"]))
    receipt = ProjectReceipt(operation_id=operation["operation_id"], write_digest="d" * 64,
        outcome="applied", terminal=True, evidence_ref="synthetic correlated receipt")
    assert operations.reconcile(conn, operation_id=operation["operation_id"],
        transport=SimpleNamespace(reconcile=lambda packet: receipt))["state"] == "confirmed"
    before = list(conn.iterdump())
    with pytest.raises(bugs.BugConflict, match="already closed"):
        gate.evidence(conn, bug["bug_id"])
    assert list(conn.iterdump()) == before
