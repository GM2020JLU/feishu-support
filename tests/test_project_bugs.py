"""Persistence/concurrency tests; no Project transport or real issue mutations."""

import sqlite3
from datetime import timedelta

import pytest

from k3_support import project_bugs as bugs
from k3_support.db import connect, integrity, migrate, migration_files
from k3_support.store import create_case
from k3_support.timeutil import iso_now, utc_now


def case(conn):
    return create_case(
        conn, title="Synthetic acceptance", case_type="bug", severity="P2", confidence=1
    )[0]


def binding(conn, case_id=None, item="123"):
    return bugs.bind(
        conn,
        case_id=case_id or case(conn),
        host="project.feishu.cn",
        project_key="resolved-space",
        type_key="resolved-type",
        item_id=item,
        actor="owner",
    )


def payload(closed=False):
    return {
        "fields": {"title": "Synthetic"},
        "status_id": "actual-state-id",
        "closure": {"closed": closed, "reason": None},
        "remote_version": None,
        "schema_digest": None,
    }


def observe(conn, bug, request="read-1", sequence=0, data=None, at=None):
    return bugs.observe(
        conn,
        bug_id=bug["bug_id"],
        observation_id=request,
        expected_sequence=sequence,
        payload=data or payload(),
        observed_at=at or iso_now(),
    )


def test_binding_is_unique_in_both_directions(conn):
    first = binding(conn)
    assert binding(conn, first["case_id"]) == first
    with pytest.raises(bugs.BugConflict):
        binding(conn, item="123")
    with pytest.raises(bugs.BugConflict):
        binding(conn, first["case_id"], item="456")
    assert conn.execute("SELECT count(*) FROM project_bugs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM project_bug_events").fetchone()[0] == 1


def test_snapshot_retries_and_late_competing_reads(conn):
    bug = binding(conn)
    timestamp = iso_now()
    first = observe(conn, bug, at=timestamp)
    assert observe(conn, bug, at=timestamp) == first
    with pytest.raises(bugs.BugConflict, match="reused"):
        observe(conn, bug, at=timestamp, data=payload(True))
    with pytest.raises(bugs.BugConflict, match="newer"):
        observe(conn, bug, request="concurrent-read")
    observe(conn, bug, request="fresh-read", sequence=1)
    assert bugs.detail(conn, bug["bug_id"])["snapshot"]["sequence"] == 2


def test_snapshot_cannot_go_backwards_or_be_rewritten(conn):
    bug = binding(conn)
    first = observe(conn, bug)
    with pytest.raises(bugs.BugConflict, match="older"):
        observe(
            conn,
            bug,
            request="late",
            sequence=1,
            at=(utc_now() - timedelta(days=1)).isoformat(),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE project_bug_snapshots SET payload_json='{}' WHERE snapshot_id=?",
            (first["snapshot_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute("DELETE FROM project_bug_snapshots")


def test_remote_closure_never_certifies_local_verification(conn):
    bug = binding(conn)
    observe(conn, bug, data=payload(True))
    round_ = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="start",
        reason="Investigate a closed historical issue",
        expected_revision=2,
    )
    result = bugs.detail(conn, bug["bug_id"])
    assert result["snapshot"]["closure"]["closed"] is True
    assert round_["verification_state"] == "not_run"
    assert round_["repair_state"] == "not_started"
    assert (
        conn.execute(
            "SELECT state FROM cases WHERE case_id=?", (bug["case_id"],)
        ).fetchone()[0]
        == "intake"
    )


def test_round_submission_replay_and_stale_control(conn):
    bug = binding(conn)
    args = {
        "bug_id": bug["bug_id"],
        "actor": "owner",
        "request_id": "one",
        "reason": "Investigate",
        "expected_revision": 1,
    }
    first = bugs.start_round(conn, **args)
    assert bugs.start_round(conn, **args) == first
    with pytest.raises(bugs.BugConflict, match="reused"):
        bugs.start_round(conn, **{**args, "reason": "Different"})
    with pytest.raises(bugs.BugConflict, match="changed"):
        bugs.start_round(conn, **{**args, "request_id": "two"})
    second = bugs.start_round(conn, **{**args, "request_id": "two", "expected_revision": 2})
    assert second['number'] == 2
    assert conn.execute('SELECT archived_at FROM project_bug_rounds WHERE round_id=?',
                        (first['round_id'],)).fetchone()[0]


@pytest.mark.parametrize("state", ["running", "paused", "human", "blocked", "unknown"])
def test_unsettled_round_cannot_be_superseded(conn, state):
    bug = binding(conn)
    first = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="one",
        reason="Investigate",
        expected_revision=1,
    )
    # Seed a trusted scheduler state; the public API intentionally cannot assert success.
    conn.execute(
        "UPDATE project_bug_rounds SET execution_state=? WHERE round_id=?",
        (state, first["round_id"]),
    )
    with pytest.raises(bugs.BugConflict, match="not settled"):
        bugs.start_round(
            conn,
            bug_id=bug["bug_id"],
            actor="owner",
            request_id="two",
            reason="Retry",
            expected_revision=2,
        )


def test_reopen_preserves_old_round_without_reusing_pass(conn):
    bug = binding(conn)
    first = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="one",
        reason="Investigate",
        expected_revision=1,
    )
    conn.execute(
        "UPDATE project_bug_rounds SET execution_state='succeeded',verification_state='passed' WHERE round_id=?",
        (first["round_id"],),
    )
    second = bugs.start_round(
        conn,
        bug_id=bug["bug_id"],
        actor="owner",
        request_id="two",
        reason="Recurrence",
        expected_revision=2,
    )
    result = bugs.detail(conn, bug["bug_id"])
    assert result["rounds"][0]["archived_at"]
    assert result["rounds"][0]["verification_state"] == "passed"
    assert second["verification_state"] == "not_run"
    assert second["number"] == 2
    assert result["snapshot"] is None  # No fabricated remote reopen or state.


def test_invalid_snapshot_is_not_persisted(conn):
    bug = binding(conn)
    for value in (
        {},
        {**payload(), "closure": {"closed": "yes", "reason": None}},
        {**payload(), "fields": {"score": float("nan")}},
    ):
        with pytest.raises(ValueError):
            observe(conn, bug, data=value if value else {"missing": True})
    assert bugs.detail(conn, bug["bug_id"])["snapshot"] is None
    assert integrity(conn)["ok"]
    assert migrate(conn) == []


def test_upgrade_from_previous_release_preserves_cases(tmp_path):
    db = connect(tmp_path / "previous-release.db")
    try:
        for version, name, sql in migration_files():
            if version > 104:
                break
            db.executescript(sql)
            db.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?)",
                (version, name, iso_now()),
            )
        case_id = case(db)
        before = dict(
            db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        )
        assert migrate(db) == [
            version for version, _, _ in migration_files() if version > 104
        ]
        assert (
            dict(
                db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
            )
            == before
        )
        bug = binding(db, case_id)
        assert bugs.detail(db, bug["bug_id"])["case_id"] == case_id
        assert migrate(db) == []
        assert integrity(db)["ok"]
    finally:
        db.close()
