"""Real repair review binding; verification verdict and broker evidence synthetic."""

from datetime import timedelta

import pytest
from test_project_repair_reviews import setup_evidence, detail, record, review_payload

from k3_support import project_close_gate as gate
from k3_support.project_bugs import BugConflict, _bug
from k3_support.timeutil import utc_now

CHANGE = {"transition_id": "to-close", "target_status_id": "closed"}


@pytest.fixture
def repair_context(conn, config, tmp_path, monkeypatch):
    cfg, task, job, _ = setup_evidence(conn, config, tmp_path)
    monkeypatch.setattr(gate, "evidence", lambda conn, bug_id: {
        "plan_id": "synthetic-passed-plan", "round_id": task["round_id"],
        "verification_state": "passed", "digest": "e" * 64,
    })
    return cfg, task, job


def request(conn, cfg, task, key="close"):
    return gate.request(conn, config=cfg, actor=cfg.control_operator_id,
                        request_id=key, bug_id=task["bug_id"], change=CHANGE,
                        expires_at=(utc_now()+timedelta(hours=1)).isoformat())


@pytest.mark.parametrize("verdict", [None, "in_progress", "not_applicable"])
def test_passed_verification_does_not_replace_coding_repair_review(conn, repair_context, verdict):
    cfg, task, _ = repair_context
    if verdict:
        record(conn, cfg, review_payload(detail(conn, cfg, task), task, verdict=verdict))
    with pytest.raises(BugConflict, match="current ready repair review"):
        request(conn, cfg, task)
    assert conn.execute("SELECT count(*) FROM project_close_approvals").fetchone()[0] == 0


@pytest.mark.parametrize("approved", [False, True])
def test_changed_repair_verdict_blocks_decision_and_consumption(conn, repair_context, approved):
    cfg, task, _ = repair_context
    reviewed = record(conn, cfg, review_payload(detail(conn, cfg, task), task))
    approval = request(conn, cfg, task)
    view = gate.detail(conn, approval["approval_id"], config=cfg)
    assert view["can_approve"]
    assert view["current_verification"]["repair_review_id"] == reviewed["review_id"]
    assert approval["verification_digest"] != "e" * 64
    decision = dict(approval_id=approval["approval_id"], actor=cfg.control_operator_id,
                    request_id="decision", approve=True,
                    expected_digest=approval["action_digest"], config=cfg)
    if approved:
        gate.decide(conn, **decision)
    record(conn, cfg, review_payload(detail(conn, cfg, task), task, verdict="in_progress"))
    assert not gate.detail(conn, approval["approval_id"], config=cfg)["can_approve"]
    with pytest.raises(BugConflict, match="current ready repair review"):
        if approved:
            gate.consume(conn, bug=_bug(conn, task["bug_id"]), change=CHANGE,
                         operation_id="not-dispatched", config=cfg)
        else:
            gate.decide(conn, **decision)
    assert conn.execute("SELECT consumed_at FROM project_close_approvals").fetchone()[0] is None
    assert conn.execute("SELECT count(*) FROM project_bug_operations").fetchone()[0] == 0


def test_reaffirmed_ready_review_requires_new_approval_binding(conn, repair_context):
    cfg, task, _ = repair_context
    record(conn, cfg, review_payload(detail(conn, cfg, task), task))
    old = request(conn, cfg, task)
    record(conn, cfg, review_payload(detail(conn, cfg, task), task))
    assert not gate.detail(conn, old["approval_id"], config=cfg)["can_approve"]
    new = request(conn, cfg, task, "replacement")
    assert new["status"] == "requested"
    assert new["verification_digest"] != old["verification_digest"]
    assert gate.detail(conn, old["approval_id"], config=cfg)["status"] == "revoked"


def test_changed_repository_configuration_invalidates_close_preview(conn, repair_context):
    cfg, task, _ = repair_context
    record(conn, cfg, review_payload(detail(conn, cfg, task), task))
    approval = request(conn, cfg, task)
    cfg.raw["repositories"][task["repository"]]["path"] = "/untrusted-replacement"
    assert not gate.detail(conn, approval["approval_id"], config=cfg)["can_approve"]
