"""Public Bug lifecycle projection over observed Project status history."""
# ruff: noqa: F811 -- isolated lifecycle and chat fixtures

import pytest
from test_project_chat_approvals import chat, setup
from test_project_close_lifecycle import observe, writer
from test_project_verification_runs import context  # noqa: F401

from k3_support import project_bug_controls, project_bugs


def _round(detail):
    return next(item for item in detail["rounds"] if item["archived_at"] is None)


def test_local_synthetic_bug_has_no_remote_project_controls(conn, context):
    cfg, bug, _run = setup(conn, context)
    conn.execute(
        "UPDATE project_bugs SET project_key='synthetic-local-only' WHERE bug_id=?",
        (bug["bug_id"],),
    )
    detail = project_bug_controls.execute(
        conn, cfg, action="detail", payload={"bug_id": bug["bug_id"]}
    )
    assert detail["host"] == "project.feishu.cn"
    assert detail["project_key"] == "synthetic-local-only"
    assert detail["grant_controls_available"] is False
    assert detail["refresh_control"] is None
    assert detail["activity_control"] is None


@pytest.mark.parametrize("channel", ["telegram", "feishu"])
def test_observed_reopen_is_projected_in_detail_and_both_chat_channels(conn, context, channel):
    cfg, bug, _run = setup(conn, context, channel)
    writer(cfg)  # closing status is explicitly configured as CLOSED
    observe(conn, bug, "OPEN", "lifecycle-open")
    reviews = [tuple(row) for row in conn.execute("SELECT * FROM project_verification_reviews ORDER BY review_id")]
    repairs = [tuple(row) for row in conn.execute("SELECT * FROM project_repair_reviews ORDER BY review_id")]

    observe(conn, bug, "CLOSED", "colleague-closed")
    observe(conn, bug, "OPEN", "colleague-reopened")

    detail = project_bug_controls.execute(conn, cfg, action="detail", payload={"bug_id": bug["bug_id"]})
    lifecycle = _round(detail)["lifecycle"]
    assert lifecycle["state"] == "reopened"
    assert lifecycle["requires_new_round"] is True
    assert lifecycle["reason"]
    assert lifecycle["source"] == "local_observed_lifecycle"
    assert [tuple(row) for row in conn.execute("SELECT * FROM project_verification_reviews ORDER BY review_id")] == reviews
    assert [tuple(row) for row in conn.execute("SELECT * FROM project_repair_reviews ORDER BY review_id")] == repairs

    response = chat(conn, cfg, f"bug {bug['bug_id']}", channel, f"reopened-detail-{channel}")
    text = response["text"]
    assert "已观测到 Bug 重新打开；本轮结论仅作历史记录" in text
    assert "验证（历史记录）" in text and "修复（历史记录）" in text
    assert f"bug start-round {bug['bug_id']}" in text


def test_ordinary_progress_stays_current_and_reopened_round_starts_clean(conn, context):
    cfg, bug, _run = setup(conn, context)
    writer(cfg)
    observe(conn, bug, "OPEN", "progress-open")
    observe(conn, bug, "IN_TEST", "ordinary-progress")
    detail = project_bug_controls.execute(conn, cfg, action="detail", payload={"bug_id": bug["bug_id"]})
    lifecycle = _round(detail)["lifecycle"]
    assert lifecycle["state"] == "current"
    assert lifecycle["requires_new_round"] is False
    assert lifecycle["source"] == "local_observed_lifecycle"

    observe(conn, bug, "CLOSED", "progress-close")
    observe(conn, bug, "OPEN", "progress-reopen")
    old_round = _round(project_bug_controls.execute(conn, cfg, action="detail", payload={"bug_id": bug["bug_id"]}))
    historical_reviews = [tuple(row) for row in conn.execute("SELECT * FROM project_verification_reviews ORDER BY review_id")]
    historical_repairs = [tuple(row) for row in conn.execute("SELECT * FROM project_repair_reviews ORDER BY review_id")]
    conn.execute("UPDATE project_bug_rounds SET execution_state='succeeded' WHERE round_id=?", (old_round["round_id"],))
    fresh = project_bugs.start_round(
        conn, bug_id=bug["bug_id"], actor=cfg.control_operator_id,
        request_id="fresh-lifecycle-round", reason="Investigate the reopened issue",
        expected_revision=project_bugs._bug(conn, bug["bug_id"])["revision"],
    )
    assert fresh["verification_state"] == "not_run"
    after = project_bug_controls.execute(conn, cfg, action="detail", payload={"bug_id": bug["bug_id"]})
    active = _round(after)
    assert active["round_id"] == fresh["round_id"]
    assert active["lifecycle"]["state"] == "current"
    assert active["lifecycle"]["requires_new_round"] is False
    assert active["verification_state"] == "not_run"
    assert [tuple(row) for row in conn.execute("SELECT * FROM project_verification_reviews ORDER BY review_id")] == historical_reviews
    assert [tuple(row) for row in conn.execute("SELECT * FROM project_repair_reviews ORDER BY review_id")] == historical_repairs
