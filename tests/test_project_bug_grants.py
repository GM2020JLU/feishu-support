"""Negative scope boundaries and durable expiry/revocation, without transports."""

import copy
import sqlite3
from datetime import timedelta

import pytest

from k3_support import project_bug_grants as grants
from k3_support import project_bugs as bugs
from k3_support.store import create_case
from k3_support.timeutil import observed_clock, utc_now


@pytest.fixture
def context(conn):
    case_id = create_case(
        conn, title="Synthetic grant", case_type="bug", severity="P2", confidence=1
    )[0]
    bug = bugs.bind(
        conn,
        case_id=case_id,
        host="project.feishu.cn",
        project_key="space",
        type_key="type",
        item_id="123",
        actor="owner",
    )
    scope = {
        "host": bug["host"],
        "project_key": "space",
        "type_key": "type",
        "bug_ids": [bug["bug_id"]],
        "actions": [
            "bug.read",
            "bug.fields",
            "bug.transition",
            "code.edit",
            "device.ram_boot",
        ],
        "fields": ["progress"],
        "transitions": ["to-test"],
        "repositories": [
            {
                "name": "uboot",
                "node": "buildhost",
                "branches": ["k3-dev"],
                "paths": ["drivers/ufs"],
            }
        ],
        "devices": [{"id": "board1", "node": "local"}],
    }
    expiry = utc_now() + timedelta(hours=1)
    grant = grants.issue(
        conn,
        actor="owner",
        request_id="one",
        scope=scope,
        expires_at=expiry.isoformat(),
    )
    target = {
        "host": bug["host"],
        "project_key": "space",
        "type_key": "type",
        "bug_id": bug["bug_id"],
        "action": "bug.read",
        "fields": [],
        "transition": None,
        "repository": None,
        "device": None,
    }
    return scope, grant, target, expiry


def covered(conn, grant, target, actor="owner"):
    return grants.covers(conn, grant_id=grant["grant_id"], actor=actor, target=target)


def test_grant_list_is_owner_scoped_and_exposes_expiry_without_extending_it(
    context, conn
):
    scope, grant, target, expiry = context
    grants.issue(
        conn,
        actor="someone-else",
        request_id="private",
        scope=scope,
        expires_at=expiry.isoformat(),
    )
    before = conn.total_changes
    result = grants.list_for_bug(
        conn, actor="owner", bug_id=target["bug_id"], after_id=""
    )
    assert [r["grant_id"] for r in result["items"]] == [grant["grant_id"]]
    assert result["items"][0]["status"] == "active"
    assert "request_digest" not in result["items"][0]
    with observed_clock(expiry):
        assert (
            grants.list_for_bug(
                conn, actor="owner", bug_id=target["bug_id"], after_id=""
            )["items"][0]["status"]
            == "expired"
        )
    assert conn.total_changes == before
    grants.revoke(conn, grant_id=grant["grant_id"], actor="owner")
    revoked = grants.list_for_bug(
        conn, actor="owner", bug_id=target["bug_id"], after_id=""
    )["items"][0]
    assert revoked["status"] == "revoked" and revoked["can_revoke"] is False


def test_grant_history_paginates_without_losing_or_repeating_ids(context, conn):
    scope, _, target, expiry = context
    for index in range(34):
        grants.issue(
            conn,
            actor="owner",
            request_id=f"history-{index}",
            scope=scope,
            expires_at=expiry.isoformat(),
        )
    first = grants.list_for_bug(
        conn, actor="owner", bug_id=target["bug_id"], after_id=""
    )
    assert len(first["items"]) == 30 and first["next_cursor"]
    second = grants.list_for_bug(
        conn, actor="owner", bug_id=target["bug_id"], after_id=first["next_cursor"]
    )
    assert len(second["items"]) == 5 and second["next_cursor"] is None
    assert len({r["grant_id"] for r in first["items"] + second["items"]}) == 35
    with pytest.raises(ValueError):
        grants.list_for_bug(conn, actor="owner", bug_id="missing", after_id="")


def test_continuous_scope_needs_no_consumption_but_expires(context, conn):
    _, grant, target, expiry = context
    assert covered(conn, grant, target)
    assert covered(conn, grant, target)
    with observed_clock(expiry):
        assert not covered(conn, grant, target)
    assert not covered(conn, grant, target, actor="other")


@pytest.mark.parametrize(
    "key,value",
    [
        ("host", "other.example"),
        ("project_key", "other"),
        ("type_key", "other"),
        ("bug_id", "other"),
    ],
)
def test_each_business_dimension_is_required(context, conn, key, value):
    _, grant, target, _ = context
    assert not covered(conn, grant, {**target, key: value})


def test_field_and_transition_permissions_are_separate(context, conn):
    _, grant, target, _ = context
    assert covered(
        conn, grant, {**target, "action": "bug.fields", "fields": ["progress"]}
    )
    assert not covered(
        conn, grant, {**target, "action": "bug.fields", "fields": ["assignee"]}
    )
    assert covered(
        conn, grant, {**target, "action": "bug.transition", "transition": "to-test"}
    )
    assert not covered(
        conn, grant, {**target, "action": "bug.close", "transition": "to-test"}
    )
    assert not covered(
        conn, grant, {**target, "action": "bug.transition", "transition": "close"}
    )


def test_repository_scope_uses_path_components_and_exact_node_branch(context, conn):
    _, grant, target, _ = context
    repo = {
        "name": "uboot",
        "node": "buildhost",
        "branch": "k3-dev",
        "paths": ["drivers/ufs/a.c"],
    }
    action = {**target, "action": "code.edit", "repository": repo}
    assert covered(conn, grant, action)
    for replacement in (
        {"paths": ["drivers/ufs-other/a.c"]},
        {"node": "local"},
        {"branch": "main"},
        {"name": "linux"},
        {"paths": ["drivers/ufs/a.c", "board/a.c"]},
    ):
        assert not covered(
            conn, grant, {**action, "repository": {**repo, **replacement}}
        )
    for path in ["drivers/ufs/../../board/a.c", "/drivers/ufs/a.c", "drivers//ufs/a.c"]:
        with pytest.raises(ValueError):
            covered(conn, grant, {**action, "repository": {**repo, "paths": [path]}})
    assert not covered(conn, grant, {**action, "action": "code.push"})


def test_ram_boot_does_not_grant_flash_or_other_devices(context, conn):
    _, grant, target, _ = context
    action = {
        **target,
        "action": "device.ram_boot",
        "device": {"id": "board1", "node": "local"},
    }
    assert covered(conn, grant, action)
    assert not covered(conn, grant, {**action, "action": "device.flash"})
    assert not covered(
        conn, grant, {**action, "device": {"id": "board2", "node": "local"}}
    )


def test_revoke_is_final_and_request_replay_cannot_renew(context, conn):
    scope, grant, target, expiry = context
    with pytest.raises(ValueError):
        grants.revoke(conn, grant_id=grant["grant_id"], actor="other")
    assert covered(conn, grant, target)
    grants.revoke(conn, grant_id=grant["grant_id"], actor="owner")
    grants.revoke(conn, grant_id=grant["grant_id"], actor="owner")
    replay = grants.issue(
        conn,
        actor="owner",
        request_id="one",
        scope=scope,
        expires_at=expiry.isoformat(),
    )
    assert replay["revoked_at"]
    assert not covered(conn, grant, target)
    assert (
        conn.execute("SELECT count(*) FROM project_bug_grant_events").fetchone()[0] == 2
    )
    with pytest.raises(sqlite3.IntegrityError, match="final"):
        conn.execute("UPDATE project_bug_grants SET revoked_at=NULL")
    with pytest.raises(bugs.BugConflict):
        grants.issue(
            conn,
            actor="owner",
            request_id="one",
            scope=scope,
            expires_at=(expiry + timedelta(hours=1)).isoformat(),
        )


def test_grant_scope_cannot_be_changed_in_place(context, conn):
    scope, _, _, expiry = context
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE project_bug_grants SET scope_json='{}'")
    bad = copy.deepcopy(scope)
    bad["project_key"] = "another-space"
    with pytest.raises(ValueError, match="does not match"):
        grants.issue(
            conn,
            actor="owner",
            request_id="two",
            scope=bad,
            expires_at=expiry.isoformat(),
        )


@pytest.mark.parametrize(
    "replacement",
    [
        {"fields": ["*"]},
        {"actions": ["admin.template"]},
        {"bug_ids": []},
        {"bug_ids": ["*"]},
        {"unexpected": True},
    ],
)
def test_incomplete_or_wildcard_grants_fail_closed(context, conn, replacement):
    scope, _, _, expiry = context
    with pytest.raises(ValueError):
        grants.issue(
            conn,
            actor="owner",
            request_id="two",
            scope={**scope, **replacement},
            expires_at=expiry.isoformat(),
        )


def test_malformed_action_is_not_authorized(context, conn):
    _, grant, target, _ = context
    for replacement in (
        {"action": "code.edit"},
        {"action": "bug.fields"},
        {"action": "device.flash"},
        {"fields": ["priority"]},
        {"action": "bug.close"},
        {"approved": True},
    ):
        with pytest.raises(ValueError):
            covered(conn, grant, {**target, **replacement})
