# ruff: noqa: F811 -- shared isolated fixtures

import pytest
from test_project_bug_operations import setup  # noqa: F401
from test_project_read_snapshot import Client
from test_project_refresh import READER
from test_project_write_transport import (
    WriteFake,
    history,
    make_transport,
    op_record,
    prepare,
    snapshot_pages,
)

from k3_support import project_activity as activity
from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_write_reconcile as reconcile
from k3_support.project_bug_controls import execute


def pending(conn, setup):
    cfg, op = prepare(conn, setup)
    cfg.raw["project_integration"]["reader"] = dict(READER)
    fake = WriteFake(snapshot_pages() + snapshot_pages() + snapshot_pages())
    transport = make_transport(
        conn, fake, reader_digest=reconcile.reader_identity(READER)
    )
    result = operations.dispatch(
        conn, cfg, operation_id=op["operation_id"], transport=transport
    )
    assert result["state"] == "unknown"
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
            "request_id": "write-reconcile-one",
        },
    )


def test_read_only_requeue_settles_unknown_write_after_indexing_delay(conn, setup):
    cfg, op, args = pending(conn, setup)
    execute(conn, cfg, action="write-reconcile", payload=args)
    client = Client(snapshot_pages(progress="new") + history(op_record()))
    result = activity.run_one(conn, lambda: cfg, client_factory=lambda _: client)
    page = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=0
    )
    item = page["items"][0]
    assert result["state"] == "succeeded" and item["state"] == "confirmed"
    assert item["result"]["evidence_ref"].startswith("project-op-record:")
    assert item["receipt_state"] == "acknowledged"
    assert not hasattr(client, "update_fields") and not hasattr(
        client, "transition_state"
    )
    settled = operations._operation(conn, op["operation_id"])
    assert settled["state"] == "confirmed"


def test_unconfirmed_remote_keeps_unknown_without_new_evidence(conn, setup):
    cfg, op, args = pending(conn, setup)
    execute(conn, cfg, action="write-reconcile", payload=args)
    client = Client(snapshot_pages() + history(op_record()))
    result = activity.run_one(conn, lambda: cfg, client_factory=lambda _: client)
    page = activity.page(
        conn, actor="owner", activity_id=result["activity_id"], offset=0
    )
    assert result["state"] == "succeeded" and page["items"][0]["state"] == "unknown"
    assert operations._operation(conn, op["operation_id"])["state"] == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"operation_id": "pbo_other"},
        {"write_digest": "not-the-digest"},
        {"extra": True},
    ],
)
def test_exact_operation_identity_is_required(conn, setup, change):
    cfg, _op, args = pending(conn, setup)
    payload = args | change
    if "extra" not in change:
        with pytest.raises((ValueError, KeyError)):
            execute(conn, cfg, action="write-reconcile", payload=payload)
    else:
        with pytest.raises(ValueError):
            execute(conn, cfg, action="write-reconcile", payload=payload)


def test_other_actor_cannot_queue_reconciliation(conn, setup):
    cfg, _op, args = pending(conn, setup)
    with pytest.raises(ValueError):
        reconcile.enqueue(
            conn,
            cfg,
            actor="someone-else",
            operation_id=args["operation_id"],
            write_digest=args["write_digest"],
            grant_id=args["grant_id"],
            request_id="foreign",
        )


def test_settled_operation_refuses_new_reconciliation(conn, setup):
    cfg, _op, args = pending(conn, setup)
    from k3_support.project_unknown_settlement import settle

    settle(
        conn,
        operation_id=args["operation_id"],
        actor="owner",
        verdict="confirmed_applied",
        evidence_text="human checked remote state and history window",
    )
    with pytest.raises(ValueError):
        execute(conn, cfg, action="write-reconcile", payload=args)


def test_projection_reports_source_and_kind(conn, setup):
    cfg, _op, args = pending(conn, setup)
    queued = execute(conn, cfg, action="write-reconcile", payload=args)
    assert queued["kind"] == "write_reconcile"
    assert queued["source"] == {
        "operation_id": args["operation_id"],
        "write_digest": args["write_digest"],
    }
