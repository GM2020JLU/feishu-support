"""Project bug create drafts preserve custody without calling providers."""

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


def scope(max_creations=5):
    return {
        "host": "project.feishu.cn",
        "project_key": "space",
        "type_key": "bug",
        "max_creations": max_creations,
    }


def issue(conn, request_id="grant", *, max_creations=5, expires_at=None):
    return grants.issue(
        conn,
        actor="owner",
        request_id=request_id,
        scope=scope(max_creations),
        expires_at=expires_at or (utc_now() + timedelta(hours=1)).isoformat(),
    )


def prepare(conn, grant, request_id="draft", *, fields=None, required=None):
    return create.prepare(
        conn,
        actor="owner",
        request_id=request_id,
        grant_id=grant["grant_id"],
        host="project.feishu.cn",
        project_key="space",
        type_key="bug",
        field_values=fields if fields is not None else {"title": "Bug title"},
        required_fields=required if required is not None else [],
    )


def ready(conn, grant, request_id):
    draft = prepare(conn, grant, request_id)
    create.attach_duplicates(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        search_id=f"search-{request_id}",
        candidates=[],
    )
    draft = create.confirm_not_duplicate(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )
    return create.mark_ready(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )


def test_prepare_replay_mismatch_missing_required_and_projection(conn):
    grant = issue(conn)
    required = [
        {"field_key": "title", "label": "Title"},
        {"field_key": "priority", "label": "Priority"},
    ]
    draft = prepare(conn, grant, required=required)
    assert create.projection(draft)["missing_required"] == ["priority"]
    replay = prepare(conn, grant, required=required)
    assert replay["draft_id"] == draft["draft_id"]
    with pytest.raises(BugConflict, match="different content"):
        prepare(conn, grant, fields={"title": "Different"}, required=required)
    with pytest.raises(PermissionError, match="outside"):
        create.prepare(
            conn,
            actor="owner",
            request_id="bad-space",
            grant_id=grant["grant_id"],
            host="project.feishu.cn",
            project_key="other",
            type_key="bug",
            field_values={"title": "Bug title"},
            required_fields=[],
        )


def test_prepare_validates_json_size_and_required_shape(conn):
    grant = issue(conn)
    with pytest.raises(ValueError, match="nonempty"):
        prepare(conn, grant, request_id="empty", fields={})
    with pytest.raises(ValueError, match="too large"):
        prepare(conn, grant, request_id="large", fields={"title": "x" * 256001})
    with pytest.raises(ValueError, match="required"):
        prepare(conn, grant, request_id="required", required=[{"field_key": "title"}])
    with pytest.raises(ValueError, match="invalid field values"):
        prepare(conn, grant, request_id="nan", fields={"title": float("nan")})


def test_attach_duplicates_resets_confirmation_and_confirm_requires_attachment(conn):
    grant = issue(conn)
    draft = prepare(conn, grant)
    with pytest.raises(ValueError, match="Duplicate|duplicate"):
        create.confirm_not_duplicate(
            conn,
            draft_id=draft["draft_id"],
            actor="owner",
            expected_digest=draft["request_digest"],
        )
    create.attach_duplicates(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        search_id="search-1",
        candidates=[{"item_id": "123", "title": "Existing"}],
    )
    confirmed = create.confirm_not_duplicate(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )
    assert create.projection(confirmed)["duplicate_confirmed"] is True
    reset = create.attach_duplicates(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        search_id="search-2",
        candidates=[],
    )
    view = create.projection(reset)
    assert view["state"] == "draft"
    assert view["duplicate_candidates"] == []
    assert view["duplicate_confirmed"] is False


def test_mark_ready_gates_missing_unconfirmed_and_expired_grant(conn):
    grant = issue(conn)
    missing = prepare(
        conn,
        grant,
        request_id="missing",
        required=[{"field_key": "priority", "label": "Priority"}],
    )
    create.attach_duplicates(
        conn,
        draft_id=missing["draft_id"],
        actor="owner",
        search_id="missing-search",
        candidates=[],
    )
    create.confirm_not_duplicate(
        conn,
        draft_id=missing["draft_id"],
        actor="owner",
        expected_digest=missing["request_digest"],
    )
    with pytest.raises(ValueError, match="required"):
        create.mark_ready(
            conn,
            draft_id=missing["draft_id"],
            actor="owner",
            expected_digest=missing["request_digest"],
        )
    unconfirmed = prepare(conn, grant, request_id="unconfirmed")
    with pytest.raises(ValueError, match="duplicate"):
        create.mark_ready(
            conn,
            draft_id=unconfirmed["draft_id"],
            actor="owner",
            expected_digest=unconfirmed["request_digest"],
        )
    expiry = (utc_now() + timedelta(hours=1)).isoformat()
    expiring_grant = issue(conn, request_id="expiring", expires_at=expiry)
    expiring = prepare(conn, expiring_grant, request_id="expiring-draft")
    create.attach_duplicates(
        conn,
        draft_id=expiring["draft_id"],
        actor="owner",
        search_id="expiring-search",
        candidates=[],
    )
    create.confirm_not_duplicate(
        conn,
        draft_id=expiring["draft_id"],
        actor="owner",
        expected_digest=expiring["request_digest"],
    )
    with observed_clock(parse_iso(expiry)), pytest.raises(PermissionError, match="outside"):
        create.mark_ready(
            conn,
            draft_id=expiring["draft_id"],
            actor="owner",
            expected_digest=expiring["request_digest"],
        )


def test_reopen_cancel_and_expected_digest_guard(conn):
    grant = issue(conn)
    draft = ready(conn, grant, "ready")
    with pytest.raises(BugConflict, match="refresh"):
        create.reopen(
            conn,
            draft_id=draft["draft_id"],
            actor="owner",
            expected_digest="0" * 64,
        )
    reopened = create.reopen(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )
    assert reopened["state"] == "draft"
    cancelled = create.cancel(
        conn,
        draft_id=draft["draft_id"],
        actor="owner",
        expected_digest=draft["request_digest"],
    )
    assert cancelled["state"] == "cancelled"
    inflight = create.reserve_dispatch(
        conn,
        draft_id=ready(conn, grant, "inflight")["draft_id"],
        actor="owner",
        expected_digest=ready(conn, grant, "inflight")["request_digest"],
    )
    with pytest.raises(BugConflict, match="in-flight creation"):
        create.cancel(
            conn,
            draft_id=inflight["draft_id"],
            actor="owner",
            expected_digest=inflight["request_digest"],
        )


def test_reserve_dispatch_enforces_budget_and_one_inflight_per_type(conn):
    grant = issue(conn, max_creations=2)
    first = ready(conn, grant, "first")
    second = ready(conn, grant, "second")
    dispatched = create.reserve_dispatch(
        conn,
        draft_id=first["draft_id"],
        actor="owner",
        expected_digest=first["request_digest"],
    )
    assert dispatched["state"] == "dispatched"
    with pytest.raises((sqlite3.IntegrityError, BugConflict)):
        create.reserve_dispatch(
            conn,
            draft_id=second["draft_id"],
            actor="owner",
            expected_digest=second["request_digest"],
        )
    create.settle_rejected(
        conn,
        draft_id=dispatched["draft_id"],
        actor="owner",
        error_code="provider_rejected",
    )

    budget_grant = issue(conn, request_id="budget", max_creations=1)
    one = ready(conn, budget_grant, "budget-one")
    two = ready(conn, budget_grant, "budget-two")
    create.reserve_dispatch(
        conn,
        draft_id=one["draft_id"],
        actor="owner",
        expected_digest=one["request_digest"],
    )
    with pytest.raises(PermissionError, match="outside"):
        create.reserve_dispatch(
            conn,
            draft_id=two["draft_id"],
            actor="owner",
            expected_digest=two["request_digest"],
        )


def test_settle_created_unknown_and_rejected_paths(conn):
    grant = issue(conn)
    created = create.reserve_dispatch(
        conn,
        draft_id=ready(conn, grant, "created")["draft_id"],
        actor="owner",
        expected_digest=ready(conn, grant, "created")["request_digest"],
    )
    with pytest.raises(ValueError, match="item"):
        create.settle_created(
            conn,
            draft_id=created["draft_id"],
            actor="owner",
            created_item_id="0",
            response_digest="response",
        )
    done = create.settle_created(
        conn,
        draft_id=created["draft_id"],
        actor="owner",
        created_item_id="12345",
        response_digest="response",
    )
    assert create.projection(done)["created_item_id"] == "12345"

    unknown = create.reserve_dispatch(
        conn,
        draft_id=ready(conn, grant, "unknown")["draft_id"],
        actor="owner",
        expected_digest=ready(conn, grant, "unknown")["request_digest"],
    )
    unknown = create.settle_unknown(conn, draft_id=unknown["draft_id"], actor="owner")
    assert unknown["state"] == "unknown"
    rejected = create.settle_rejected(
        conn,
        draft_id=unknown["draft_id"],
        actor="owner",
        error_code="provider_rejected",
    )
    assert create.projection(rejected)["error_code"] == "provider_rejected"

    direct_reject = create.reserve_dispatch(
        conn,
        draft_id=ready(conn, grant, "direct-reject")["draft_id"],
        actor="owner",
        expected_digest=ready(conn, grant, "direct-reject")["request_digest"],
    )
    assert (
        create.settle_rejected(
            conn,
            draft_id=direct_reject["draft_id"],
            actor="owner",
            error_code="permission_denied",
        )["state"]
        == "rejected"
    )
    with pytest.raises(ValueError, match="unsupported"):
        create.settle_rejected(
            conn,
            draft_id=direct_reject["draft_id"],
            actor="owner",
            error_code="other",
        )
    assert (
        conn.execute(
            """SELECT count(*) FROM project_create_grant_events
            WHERE kind='creation_settled'"""
        ).fetchone()[0]
        == 1
    )


def test_terminal_immutability_and_retention_triggers(conn):
    grant = issue(conn)
    draft = create.cancel(
        conn,
        draft_id=prepare(conn, grant)["draft_id"],
        actor="owner",
        expected_digest=prepare(conn, grant)["request_digest"],
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE project_bug_create_drafts SET updated_at=updated_at WHERE draft_id=?",
            (draft["draft_id"],),
        )
    live = prepare(conn, grant, request_id="identity")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE project_bug_create_drafts SET host='other' WHERE draft_id=?",
            (live["draft_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="retention"):
        conn.execute(
            "DELETE FROM project_bug_create_drafts WHERE draft_id=?",
            (live["draft_id"],),
        )


def test_operator_settlement_requires_unknown_and_matching_digest(conn):
    grant = issue(conn)
    live = ready(conn, grant, "operator-missing")
    with pytest.raises(BugConflict, match="unknown"):
        create.operator_settle_missing(
            conn,
            draft_id=live["draft_id"],
            actor="owner",
            expected_digest=live["request_digest"],
        )
    create.reserve_dispatch(
        conn,
        draft_id=live["draft_id"],
        actor="owner",
        expected_digest=live["request_digest"],
    )
    unknown = create.settle_unknown(conn, draft_id=live["draft_id"], actor="owner")
    with pytest.raises(BugConflict, match="changed"):
        create.operator_settle_missing(
            conn, draft_id=live["draft_id"], actor="owner", expected_digest="wrong"
        )
    settled = create.operator_settle_missing(
        conn,
        draft_id=live["draft_id"],
        actor="owner",
        expected_digest=unknown["request_digest"],
    )
    assert settled["state"] == "rejected"
    assert settled["error_code"] == "operator_verified_absent"
    # The scope is free again: a new draft can reserve its own dispatch.
    retry = ready(conn, grant, "operator-retry")
    create.reserve_dispatch(
        conn,
        draft_id=retry["draft_id"],
        actor="owner",
        expected_digest=retry["request_digest"],
    )


def test_operator_settlement_found_records_the_verified_item(conn):
    grant = issue(conn)
    live = ready(conn, grant, "operator-found")
    create.reserve_dispatch(
        conn,
        draft_id=live["draft_id"],
        actor="owner",
        expected_digest=live["request_digest"],
    )
    unknown = create.settle_unknown(conn, draft_id=live["draft_id"], actor="owner")
    settled = create.operator_settle_found(
        conn,
        draft_id=live["draft_id"],
        actor="owner",
        expected_digest=unknown["request_digest"],
        created_item_id="7123450001",
    )
    assert settled["state"] == "created"
    assert settled["created_item_id"] == "7123450001"


@pytest.mark.parametrize('value', [None, '', '  \t', [], {}])
def test_empty_required_value_cannot_make_a_ready_draft(conn, value):
    grant = issue(conn)
    draft = prepare(conn, grant, fields={'title': value}, required=[{'field_key': 'title', 'label': 'Title'}])
    assert create.projection(draft)['missing_required'] == ['title']
    create.attach_duplicates(conn, draft_id=draft['draft_id'], actor='owner', search_id='search', candidates=[])
    create.confirm_not_duplicate(conn, draft_id=draft['draft_id'], actor='owner', expected_digest=draft['request_digest'])
    with pytest.raises(ValueError, match='required fields'):
        create.mark_ready(conn, draft_id=draft['draft_id'], actor='owner', expected_digest=draft['request_digest'])


@pytest.mark.parametrize('value', [0, False])
def test_required_numeric_zero_or_boolean_false_is_not_empty(conn, value):
    draft = prepare(conn, issue(conn), fields={'flag': value}, required=[{'field_key': 'flag', 'label': 'Flag'}])
    assert create.projection(draft)['missing_required'] == []
