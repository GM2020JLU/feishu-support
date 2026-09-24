from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from k3_support.approvals import request_approval
from k3_support.db import connect, integrity, migrate, migration_files
from k3_support.ids import new_id
from k3_support.store import (
    ConflictError,
    claim_jobs,
    create_case,
    ingest_event,
    merge_case,
    reclaim_stale_jobs,
    renew_job_lease,
    transition_case,
)


def test_migration_is_idempotent_and_schema_is_healthy(conn):
    assert migrate(conn) == []
    assert integrity(conn) == {
        "quick_check": "ok",
        "foreign_key_errors": [],
        "ok": True,
    }
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert {
        "inbound_events",
        "cases",
        "case_events",
        "jobs",
        "approvals",
        "outbox",
        "knowledge_fts",
        "conversation_turns",
        "operator_activities",
        "global_control_state",
        "global_control_events",
        "global_control_panels",
    } <= tables


def test_cooperative_preemption_migrates_an_existing_v10_database(tmp_path):
    conn = connect(tmp_path / "upgrade.db")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version, name, sql in migration_files():
        if version > 10:
            break
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (version, name, datetime.now(UTC).isoformat()),
        )
    assert migrate(conn) == [version for version, _, _ in migration_files() if version > 10]
    columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    assert {
        "turn_id",
        "turn_revision",
        "communication_fence",
        "not_before",
        "suppression_reason",
        "global_outbound_fence",
    } <= columns
    route_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(route_decisions)")
    }
    assert {
        "conversation_relation",
        "conversation_case_id",
        "knowledge_id",
        "knowledge_source_digest",
        "knowledge_match_confidence",
    } <= route_columns
    assert integrity(conn)["ok"] is True
    conn.close()


def test_repeatable_approval_migration_preserves_existing_meeting_links(tmp_path):
    db = connect(tmp_path / "approval-upgrade.db")
    db.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version, name, sql in migration_files():
        if version > 12:
            break
        db.executescript(sql)
        db.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (version, name, datetime.now(UTC).isoformat()),
        )
    case_id, _ = create_case(
        db, title="meeting", case_type="meeting", severity="P3", confidence=0.8
    )
    approval_id, _, _ = request_approval(
        db,
        approval_type="meeting_create",
        case_id=case_id,
        action={"case_id": case_id, "summary": "same"},
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    db.execute(
        """INSERT INTO meeting_previews(preview_id,case_id,action_json,action_digest,
               status,approval_id,created_at,updated_at)
           SELECT 'preview-old',case_id,requested_action_json,action_digest,
                  'failed',approval_id,requested_at,updated_at
             FROM approvals WHERE approval_id=?""",
        (approval_id,),
    )
    db.execute(
        "UPDATE approvals SET status='consumed',consumed_at=? WHERE approval_id=?",
        (datetime.now(UTC).isoformat(), approval_id),
    )

    assert migrate(db) == [version for version, _, _ in migration_files() if version > 12]
    assert integrity(db)["ok"] is True
    assert (
        db.execute(
            "SELECT approval_id FROM meeting_previews WHERE preview_id='preview-old'"
        ).fetchone()[0]
        == approval_id
    )
    new_id, _, created = request_approval(
        db,
        approval_type="meeting_create",
        case_id=case_id,
        action={"case_id": case_id, "summary": "same"},
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    assert created is True
    assert new_id != approval_id
    db.close()


def test_source_retry_migration_preserves_existing_observations(tmp_path):
    db = connect(tmp_path / "refresh-upgrade.db")
    try:
        db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT,applied_at TEXT)")
        for version, name, sql in migration_files():
            if version > 22:
                break
            db.executescript(sql)
            db.execute("INSERT INTO schema_migrations VALUES(?,?,?)",
                       (version, name, datetime.now(UTC).isoformat()))
        db.execute("""INSERT INTO source_registry(source_id,source_type,stable_external_id,
                   source_version,content_digest,last_checked_at)
                   VALUES('srg_old','feishu_doc','wiki:old','12',?,'2026-09-01T00:00:00+00:00')""",
                   ("a" * 64,))
        before = tuple(db.execute("SELECT * FROM source_registry").fetchone())
        assert migrate(db) == [version for version, _, _ in migration_files() if version > 22]
        assert tuple(db.execute("SELECT * FROM source_registry").fetchone()) == before
        assert db.execute("SELECT count(*) FROM source_refresh_state").fetchone()[0] == 0
        assert migrate(db) == []
        assert integrity(db)["ok"] is True
    finally:
        db.close()


def test_replayed_event_is_inserted_once(conn):
    occurred_at = datetime.now(UTC).isoformat()
    first = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_123",
        payload={"text": "help"},
        occurred_at=occurred_at,
    )
    second = ingest_event(
        conn,
        source="feishu_bot_im",
        identity="bot",
        external_id="om_123",
        payload={"text": "changed replay must not overwrite"},
        occurred_at=occurred_at,
    )
    assert first[1] is True
    assert second == (first[0], False)
    assert conn.execute("SELECT count(*) FROM inbound_events").fetchone()[0] == 1


def test_case_create_and_transition_are_idempotent_and_versioned(conn):
    case_id, created = create_case(
        conn,
        title="Boot failure",
        case_type="bug",
        severity="P2",
        confidence=0.4,
        idempotency_key="case-from-om-1",
    )
    assert created
    assert create_case(
        conn,
        title="ignored",
        case_type="faq",
        severity="P3",
        confidence=0.9,
        idempotency_key="case-from-om-1",
    ) == (case_id, False)
    assert (
        transition_case(
            conn,
            case_id=case_id,
            after="triage",
            actor_type="system",
            actor_id=None,
            reason="normalized",
            expected_version=1,
            idempotency_key="transition-1",
        )
        == 2
    )
    assert (
        transition_case(
            conn,
            case_id=case_id,
            after="triage",
            actor_type="system",
            actor_id=None,
            reason="replayed",
            expected_version=1,
            idempotency_key="transition-1",
        )
        == 2
    )
    with pytest.raises(ConflictError):
        transition_case(
            conn,
            case_id=case_id,
            after="investigating",
            actor_type="system",
            actor_id=None,
            reason="stale worker",
            expected_version=1,
        )


def test_illegal_transition_rolls_back(conn):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    with pytest.raises(ValueError):
        transition_case(
            conn,
            case_id=case_id,
            after="resolved",
            actor_type="system",
            actor_id=None,
            reason="skip state machine",
            expected_version=1,
        )
    row = conn.execute(
        "SELECT state,version FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    assert tuple(row) == ("intake", 1)
    assert (
        conn.execute(
            "SELECT count(*) FROM case_events WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        == 1
    )


def test_merge_case_links_sources_and_preserves_duplicate_history(conn):
    canonical_id, _ = create_case(
        conn,
        title="first fragment",
        case_type="investigation",
        severity="P3",
        confidence=0.4,
    )
    duplicate_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_duplicate",
        payload={"content": "follow-up"},
        occurred_at=datetime.now(UTC).isoformat(),
    )
    duplicate_id, _ = create_case(
        conn,
        title="follow-up",
        case_type="investigation",
        severity="P3",
        confidence=0.4,
        source_event_pk=duplicate_event,
    )
    transition_case(
        conn,
        case_id=duplicate_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="classified",
        expected_version=1,
    )

    version = merge_case(
        conn,
        case_id=duplicate_id,
        canonical_case_id=canonical_id,
        actor_type="system",
        actor_id="repair",
        reason="same P2P continuation",
        expected_version=2,
        idempotency_key="merge-1",
    )

    assert version == 3
    duplicate = conn.execute(
        "SELECT state,canonical_case_id,version FROM cases WHERE case_id=?",
        (duplicate_id,),
    ).fetchone()
    assert tuple(duplicate) == ("cancelled", canonical_id, 3)
    link = conn.execute(
        """SELECT event_type,source_event_pk FROM case_events
           WHERE case_id=? AND event_type='duplicate_source_linked'""",
        (canonical_id,),
    ).fetchone()
    assert tuple(link) == ("duplicate_source_linked", duplicate_event)
    assert (
        conn.execute(
            "SELECT count(*) FROM case_events WHERE case_id=?", (duplicate_id,)
        ).fetchone()[0]
        == 3
    )
    assert (
        merge_case(
            conn,
            case_id=duplicate_id,
            canonical_case_id=canonical_id,
            actor_type="system",
            actor_id="repair",
            reason="replayed",
            expected_version=2,
            idempotency_key="merge-1",
        )
        == 3
    )


def test_merge_case_rejects_an_unconsumed_requested_approval(conn):
    canonical_id, _ = create_case(
        conn, title="canonical", case_type="bug", severity="P2", confidence=0.4
    )
    duplicate_id, _ = create_case(
        conn, title="duplicate", case_type="bug", severity="P2", confidence=0.4
    )
    request_approval(
        conn,
        approval_type="board1_lease",
        case_id=duplicate_id,
        session_id="session-active",
        action={"case_id": duplicate_id, "session_id": "session-active"},
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )

    with pytest.raises(ConflictError, match="active approval"):
        merge_case(
            conn,
            case_id=duplicate_id,
            canonical_case_id=canonical_id,
            actor_type="system",
            actor_id="repair",
            reason="must not merge through a gate",
            expected_version=1,
            idempotency_key="merge-active-approval",
        )
    row = conn.execute(
        "SELECT state,canonical_case_id,version FROM cases WHERE case_id=?",
        (duplicate_id,),
    ).fetchone()
    assert tuple(row) == ("intake", None, 1)


def test_reconcile_marks_expired_running_job_orphaned(conn):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    job_id = new_id("job")
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,input_digest,
               available_at,created_at,updated_at) VALUES(?,?,'codex','running','worker-1',?,'digest',?,?,?)""",
        (job_id, case_id, old, old, old, old),
    )
    assert reclaim_stale_jobs(conn) == [job_id]
    row = conn.execute(
        "SELECT state,error_class,lease_owner FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert tuple(row) == ("orphaned", "stale_lease", None)


def test_job_lease_renewal_is_owner_fenced(conn):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    now = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)
    job_id = new_id("job")
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,lease_owner,lease_expires_at,
               input_digest,available_at,created_at,updated_at)
           VALUES(?,?,'codex','running','worker-1',?,'digest',?,?,?)""",
        (job_id, case_id, now.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
    )

    assert not renew_job_lease(
        conn,
        job_id=job_id,
        worker_id="worker-2",
        now=now + timedelta(seconds=30),
    )
    assert renew_job_lease(
        conn,
        job_id=job_id,
        worker_id="worker-1",
        lease_seconds=120,
        now=now + timedelta(seconds=30),
    )
    row = conn.execute(
        "SELECT heartbeat_at,lease_expires_at FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert row["heartbeat_at"] == (now + timedelta(seconds=30)).isoformat()
    assert row["lease_expires_at"] == (now + timedelta(seconds=150)).isoformat()


def test_job_claim_respects_case_pause(conn):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="classified",
        expected_version=1,
    )
    now = datetime.now(UTC).isoformat()
    job_id = new_id("job")
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
           VALUES(?,?,'codex','queued','digest',?,?,?)""",
        (job_id, case_id, now, now, now),
    )
    assert [item["job_id"] for item in claim_jobs(conn, "worker")] == [job_id]
    conn.execute(
        "UPDATE jobs SET state='queued',lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?",
        (job_id,),
    )
    transition_case(
        conn,
        case_id=case_id,
        after="paused",
        actor_type="operator",
        actor_id="owner",
        reason="take control",
        expected_version=2,
    )
    assert claim_jobs(conn, "worker") == []


def test_base_sync_job_can_run_without_case_and_does_not_bypass_other_job_gates(conn):
    now = datetime.now(UTC).isoformat()
    base_job = new_id("job")
    conn.execute(
        """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,
               created_at,updated_at,context_json)
           VALUES(?,NULL,'base_sync','queued','base-digest',?,?,?,?)""",
        (
            base_job,
            now,
            now,
            now,
            '{"entity_id":"worker","entity_type":"health"}',
        ),
    )
    claimed = claim_jobs(conn, "base-worker", job_types=("base_sync",))
    assert [row["job_id"] for row in claimed] == [base_job]
