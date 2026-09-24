from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from k3_support.approvals import request_approval
from k3_support.config import Config
from k3_support.store import create_case, transition_case
from k3_support.workbench import (
    professional_knowledge_snapshot,
    render_workbench,
    workbench_snapshot,
)


def test_workbench_unifies_owner_ai_approval_and_failure_queues(conn):
    now = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)
    owner_case, _ = create_case(
        conn,
        title="需要确定发布优先级",
        case_type="request",
        severity="P1",
        confidence=0.9,
    )
    transition_case(
        conn,
        case_id=owner_case,
        after="escalated",
        actor_type="hermes",
        actor_id="hermes",
        reason="owner decision",
        expected_version=1,
        idempotency_key="workbench-owner",
    )
    ai_case, _ = create_case(
        conn, title="启动失败排查", case_type="bug", severity="P2", confidence=0.8
    )
    transition_case(
        conn,
        case_id=ai_case,
        after="triage",
        actor_type="hermes",
        actor_id="hermes",
        reason="triage",
        expected_version=1,
        idempotency_key="workbench-ai-triage",
    )
    transition_case(
        conn,
        case_id=ai_case,
        after="investigating",
        actor_type="hermes",
        actor_id="hermes",
        reason="debug",
        expected_version=2,
        idempotency_key="workbench-ai-debug",
    )
    request_approval(
        conn,
        approval_type="board1_lease",
        case_id=ai_case,
        action={"case_id": ai_case, "minutes": 15},
        expires_at=(now + timedelta(minutes=15)).isoformat(),
    )
    conn.execute(
        "UPDATE cases SET last_material_progress_at=? WHERE case_id=?",
        ((now - timedelta(minutes=90)).isoformat(), ai_case),
    )
    conn.execute(
        """INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
               idempotency_key,state,created_at,updated_at,global_outbound_fence)
           VALUES('out_failed','telegram','notify','telegram:owner','{}','failed-workbench',
                  'permanent_failure',?,?,1)""",
        (now.isoformat(), now.isoformat()),
    )

    snapshot = workbench_snapshot(conn, now=now)

    assert snapshot["counts"]["owner_decision"] == 1
    assert snapshot["counts"]["ai_processing"] == 1
    assert snapshot["counts"]["approval"] == 1
    assert snapshot["counts"]["delivery_failure"] == 1
    assert snapshot["counts"]["overdue"] == 1
    rendered = render_workbench(snapshot)
    assert owner_case in rendered
    assert ai_case in rendered
    assert "⚠️超时" in rendered
    assert "待审批 1" in rendered


def test_workbench_hides_failures_for_disabled_urgent_channels(conn, config: Config):
    now = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)
    conn.execute(
        """INSERT INTO outbox(outbox_id,channel,action_type,destination,payload_json,
               idempotency_key,state,created_at,updated_at,global_outbound_fence)
           VALUES('out_old_sms','feishu_urgent_sms','urgent','ou_owner','{}','old-sms',
                  'permanent_failure',?,?,1)""",
        (now.isoformat(), now.isoformat()),
    )

    snapshot = workbench_snapshot(conn, config=config, now=now)
    assert snapshot["counts"]["delivery_failure"] == 0
    assert "out_old_sms" not in render_workbench(snapshot)


def test_professional_workbench_reads_legacy_review_database_without_migrating(
    tmp_path,
):
    database = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database)
    legacy.row_factory = sqlite3.Row
    legacy.execute("CREATE TABLE knowledge_entries(knowledge_id TEXT,status TEXT)")
    legacy.execute("INSERT INTO knowledge_entries VALUES('knw_old','approved')")
    legacy.commit()
    before = database.read_bytes()

    report = professional_knowledge_snapshot(
        legacy, repository_root=tmp_path / "knowledge"
    )

    assert report["database"]["legacy_approved"] == 1
    assert report["database"]["schema_supports_professional"] is False
    assert report["ready_for_automatic_reply"] is False
    assert database.read_bytes() == before
