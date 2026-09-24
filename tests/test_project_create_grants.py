"""Creation grants are exact, bounded, replayable, and durable."""

import sqlite3
from datetime import timedelta

import pytest

from k3_support import project_bug_create as create
from k3_support import project_create_grants as grants
from k3_support.db import migrate
from k3_support.project_bugs import BugConflict
from k3_support.timeutil import observed_clock, parse_iso, utc_now


def open_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@pytest.fixture
def conn():
    db = open_db()
    migrate(db)
    try:
        yield db
    finally:
        db.close()


def scope(max_creations=2):
    return {
        "host": "project.feishu.cn",
        "project_key": "space",
        "type_key": "bug",
        "max_creations": max_creations,
    }


def issue(conn, request_id="grant", *, max_creations=2):
    return grants.issue(
        conn,
        actor="owner",
        request_id=request_id,
        scope=scope(max_creations),
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )


def prepare(conn, grant, request_id, title="Bug"):
    return create.prepare(
        conn,
        actor="owner",
        request_id=request_id,
        grant_id=grant["grant_id"],
        host="project.feishu.cn",
        project_key="space",
        type_key="bug",
        field_values={"title": title},
        required_fields=[],
    )


def test_migrations_apply_and_second_run_is_empty():
    db = open_db()
    try:
        versions = migrate(db)
        assert 130 in versions and 131 in versions
        assert migrate(db) == []
    finally:
        db.close()


def test_issue_replay_digest_mismatch_expiry_and_revoke(conn):
    grant = issue(conn)
    replay = grants.issue(
        conn,
        actor="owner",
        request_id="grant",
        scope=scope(),
        expires_at=grant["expires_at"],
    )
    assert replay["grant_id"] == grant["grant_id"]
    with pytest.raises(BugConflict, match="different content"):
        grants.issue(
            conn,
            actor="owner",
            request_id="grant",
            scope=scope(3),
            expires_at=grant["expires_at"],
        )
    with pytest.raises(ValueError, match="future"):
        grants.issue(
            conn,
            actor="owner",
            request_id="past",
            scope=scope(),
            expires_at=(utc_now() - timedelta(seconds=1)).isoformat(),
        )
    with pytest.raises(ValueError, match="owned"):
        grants.revoke(conn, grant_id=grant["grant_id"], actor="other")
    grants.revoke(conn, grant_id=grant["grant_id"], actor="owner")
    grants.revoke(conn, grant_id=grant["grant_id"], actor="owner")
    assert not grants.covers(
        conn,
        grant_id=grant["grant_id"],
        actor="owner",
        host="project.feishu.cn",
        project_key="space",
        type_key="bug",
    )
    assert conn.execute("SELECT count(*) FROM project_create_grant_events").fetchone()[0] == 2


def test_validate_scope_rejects_inexact_wildcard_and_bad_budget():
    for replacement in (
        {"host": "*"},
        {"max_creations": 0},
        {"max_creations": 21},
        {"max_creations": True},
        {"extra": "value"},
    ):
        with pytest.raises(ValueError):
            grants.validate_scope({**scope(), **replacement})


def test_covers_false_on_each_failure_axis(conn):
    grant = issue(conn)
    assert grants.covers(
        conn,
        grant_id=grant["grant_id"],
        actor="owner",
        host="project.feishu.cn",
        project_key="space",
        type_key="bug",
    )
    axes = (
        {"actor": "other"},
        {"host": "other.example"},
        {"project_key": "other"},
        {"type_key": "other"},
    )
    for replacement in axes:
        kwargs = {
            "grant_id": grant["grant_id"],
            "actor": "owner",
            "host": "project.feishu.cn",
            "project_key": "space",
            "type_key": "bug",
        }
        kwargs.update(replacement)
        assert not grants.covers(conn, **kwargs)
    with observed_clock(parse_iso(grant["expires_at"])):
        assert not grants.covers(
            conn,
            grant_id=grant["grant_id"],
            actor="owner",
            host="project.feishu.cn",
            project_key="space",
            type_key="bug",
        )
    assert not grants.covers(
        conn,
        grant_id="pcg_missing",
        actor="owner",
        host="project.feishu.cn",
        project_key="space",
        type_key="bug",
    )


def test_budget_counts_only_consumed_or_possibly_consumed_states(conn):
    grant = issue(conn, max_creations=2)
    drafts = [prepare(conn, grant, f"draft-{state}", state) for state in ("a", "b", "c")]
    conn.execute(
        "UPDATE project_bug_create_drafts SET state='draft' WHERE draft_id=?",
        (drafts[0]["draft_id"],),
    )
    conn.execute(
        "UPDATE project_bug_create_drafts SET state='dispatched' WHERE draft_id=?",
        (drafts[1]["draft_id"],),
    )
    conn.execute(
        """UPDATE project_bug_create_drafts
        SET state='created', created_item_id='123' WHERE draft_id=?""",
        (drafts[2]["draft_id"],),
    )
    assert grants.remaining_budget(conn, grant["grant_id"]) == 0
    assert not grants.covers(
        conn,
        grant_id=grant["grant_id"],
        actor="owner",
        host="project.feishu.cn",
        project_key="space",
        type_key="bug",
    )
    conn.execute(
        """UPDATE project_bug_create_drafts
        SET state='ready' WHERE draft_id=?""",
        (drafts[1]["draft_id"],),
    )
    assert grants.remaining_budget(conn, grant["grant_id"]) == 1
    assert grants.projection(
        conn.execute(
            """SELECT g.*,
            (SELECT count(*) FROM project_bug_create_drafts d
             WHERE d.grant_id=g.grant_id AND d.state IN ('dispatched','unknown','created')) AS used
             FROM project_create_grants g WHERE grant_id=?""",
            (grant["grant_id"],),
        ).fetchone()
    )["remaining"] == 1


def test_list_for_actor_paginates_and_omits_other_actors(conn):
    issue(conn, request_id="first")
    for index in range(34):
        issue(conn, request_id=f"history-{index}")
    grants.issue(
        conn,
        actor="other",
        request_id="private",
        scope=scope(),
        expires_at=(utc_now() + timedelta(hours=1)).isoformat(),
    )
    page = grants.list_for_actor(conn, actor="owner", after_id="")
    assert len(page["items"]) == 30 and page["next_cursor"]
    assert "request_digest" not in page["items"][0]
    rest = grants.list_for_actor(conn, actor="owner", after_id=page["next_cursor"])
    assert len(rest["items"]) == 5 and rest["next_cursor"] is None


def test_immutability_and_retention_triggers(conn):
    grant = issue(conn)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE project_create_grants SET host='other' WHERE grant_id=?",
            (grant["grant_id"],),
        )
    grants.revoke(conn, grant_id=grant["grant_id"], actor="owner")
    with pytest.raises(sqlite3.IntegrityError, match="final"):
        conn.execute(
            "UPDATE project_create_grants SET revoked_at=NULL WHERE grant_id=?",
            (grant["grant_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute("DELETE FROM project_create_grants WHERE grant_id=?", (grant["grant_id"],))
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute("DELETE FROM project_create_grant_events")
