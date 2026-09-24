# ruff: noqa: F811 -- shared isolated fixtures
import json
import sqlite3

import pytest
from test_project_bug_operations import setup  # noqa: F401
from test_project_comment_transport import comments, prepare, snapshot_pages
from test_project_read_snapshot import Client
from test_project_refresh import READER

from k3_support import project_activity as activity
from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_comment_reconcile as reconcile
from k3_support.project_bug_controls import execute


def pending(conn, setup):
    cfg, op, transport, _fake = prepare(conn, setup, observed=None)
    cfg.raw["project_integration"]["reader"] = dict(READER)
    transport.reader_digest = reconcile.reader_identity(READER)
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "unknown"
    # A separate current read grant is independent of the earlier write grant.
    bug = conn.execute(
        "SELECT * FROM project_bugs WHERE bug_id=?", (op["bug_id"],)
    ).fetchone()
    scope = {k: bug[k] for k in ("host", "project_key", "type_key")}
    scope.update(
        bug_ids=[bug["bug_id"]],
        actions=["bug.read"],
        fields=[],
        transitions=[],
        repositories=[],
        devices=[],
    )
    old = conn.execute(
        "SELECT expires_at FROM project_bug_grants WHERE grant_id=?", (op["grant_id"],)
    ).fetchone()
    grant = grants.issue(
        conn, actor="owner", request_id="read-grant", scope=scope, expires_at=old[0]
    )
    grants.revoke(conn, actor="owner", grant_id=op["grant_id"])
    cfg.raw["project_integration"]["write_enabled"] = False
    return (
        cfg,
        result,
        {
            "operation_id": op["operation_id"],
            "write_digest": result["write_digest"],
            "grant_id": grant["grant_id"],
            "request_id": "reconcile-one",
        },
    )


def test_exact_read_grant_can_reconcile_after_write_revocation_without_writer(
    conn, setup
):
    cfg, op, args = pending(conn, setup)
    queued = execute(conn, cfg, action="comment-reconcile", payload=args)
    client = Client(snapshot_pages() + comments("Reviewed conclusion"))
    assert not hasattr(client, "create_comment")
    result = activity.run_one(conn, lambda: cfg, client_factory=lambda _: client)
    assert result["state"] == "succeeded"
    assert operations._operation(conn, op["operation_id"])["state"] == "confirmed"
    assert result["source"] == {k: args[k] for k in ("operation_id", "write_digest")}
    page = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=0
    )
    assert (
        page["items"][0]["state"] == "confirmed"
        and page["observation"]["write_performed"] is False
    )
    assert (
        execute(conn, cfg, action="comment-reconcile", payload=args)["activity_id"]
        == queued["activity_id"]
    )
    with pytest.raises(ValueError):
        execute(
            conn,
            cfg,
            action="comment-reconcile",
            payload=args | {"request_id": "after-terminal"},
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE project_activity_requests SET source_json='{}' WHERE activity_id=?",
            (result["activity_id"],),
        )


@pytest.mark.parametrize(
    "change",
    [{"write_digest": "forged"}, {"operation_id": "other"}, {"profile": "injected"}],
)
def test_browser_cannot_change_destination_fingerprint_or_identity(conn, setup, change):
    cfg, _op, args = pending(conn, setup)
    with pytest.raises(ValueError):
        execute(conn, cfg, action="comment-reconcile", payload=args | change)
    assert (
        conn.execute(
            "SELECT count(*) FROM project_activity_requests WHERE kind='comment_reconcile'"
        ).fetchone()[0]
        == 0
    )


def test_other_actor_cannot_queue_or_read_operation_result(conn, setup):
    cfg, _op, args = pending(conn, setup)
    with pytest.raises(ValueError):
        reconcile.enqueue(conn, cfg, actor="other", **args)


def test_read_revocation_during_remote_lookup_prevents_settlement(conn, setup):
    cfg, op, args = pending(conn, setup)
    queued = execute(conn, cfg, action="comment-reconcile", payload=args)
    before = conn.execute(
        "SELECT count(*) FROM project_bug_operation_observations"
    ).fetchone()[0]

    def hook(number):
        if number == 1:
            grants.revoke(conn, actor="owner", grant_id=args["grant_id"])

    client = Client(snapshot_pages() + comments("Reviewed conclusion"), hook=hook)
    result = activity.run_one(conn, lambda: cfg, client_factory=lambda _: client)
    assert result["state"] == "blocked"
    assert operations._operation(conn, op["operation_id"])["state"] == "unknown"
    assert (
        conn.execute(
            "SELECT count(*) FROM project_bug_operation_observations"
        ).fetchone()[0]
        == before
    )
    assert (
        activity.status(conn, actor="owner", activity_id=queued["activity_id"])[
            "observation"
        ]
        is None
    )


def test_lost_queue_lease_cannot_settle_remote_observation(conn, setup):
    cfg, op, args = pending(conn, setup)
    queued = execute(conn, cfg, action="comment-reconcile", payload=args)
    before = conn.execute(
        "SELECT count(*) FROM project_bug_operation_observations"
    ).fetchone()[0]

    def hook(number):
        if number == 1:
            conn.execute(
                "UPDATE project_activity_requests SET lease_token='replacement' WHERE activity_id=?",
                (queued["activity_id"],),
            )

    activity.run_one(
        conn, lambda: cfg, client_factory=lambda _: Client(snapshot_pages(), hook=hook)
    )
    assert operations._operation(conn, op["operation_id"])["state"] == "unknown"
    assert (
        conn.execute(
            "SELECT count(*) FROM project_bug_operation_observations"
        ).fetchone()[0]
        == before
    )


def test_profile_change_before_processing_blocks_queue(conn, setup):
    cfg, _op, args = pending(conn, setup)
    execute(conn, cfg, action="comment-reconcile", payload=args)
    cfg.raw["project_integration"]["reader"]["profile"] = "different"
    client = Client([])
    result = activity.run_one(conn, lambda: cfg, client_factory=lambda _: client)
    assert result["state"] == "blocked" and client.calls == []


def test_no_matching_comment_keeps_unknown_and_reports_receipt_boundary(conn, setup):
    cfg, op, args = pending(conn, setup)
    execute(conn, cfg, action="comment-reconcile", payload=args)
    client = Client(snapshot_pages() + comments())
    result = activity.run_one(conn, lambda: cfg, client_factory=lambda _: client)
    page = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=0
    )
    assert result["state"] == "succeeded" and page["items"][0]["state"] == "unknown"
    assert page["items"][0]["receipt_state"] == "acknowledged"
    assert json.loads(
        operations._operation(conn, op["operation_id"])["result_json"]
    ) == {"outcome": "unknown"}
