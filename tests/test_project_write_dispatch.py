# ruff: noqa: F811 -- shared isolated fixtures
"""Write dispatch queue custody, contract gating and controls; no live Project."""

import sqlite3

import pytest
from test_project_bug_operations import setup  # noqa: F401
from test_project_refresh import READER
from test_project_write_transport import (
    WriteFake,
    history,
    op_record,
    prepare,
    snapshot_pages,
    transition_page,
    transition_row,
)

from k3_support import project_bug_grants as grants
from k3_support import project_bug_operations as operations
from k3_support import project_field_writer_config as policy
from k3_support import project_write_dispatch as dispatch
from k3_support.project_bug_controls import execute

ACCEPTED_SHA256 = "ffdc86254fe40962efc1b83b7c4f098694b76d9ba92bf92eaef46cf0f5d78362"


def happy_pages():
    return (
        snapshot_pages()
        + [transition_page(transition_row()), {}]
        + snapshot_pages()
        + snapshot_pages(progress="new")
        + history(op_record(operator="dedicated-user"))
    )


def ready(
    conn,
    setup,
    monkeypatch,
    *,
    accepted=True,
    risk_policy="recheck_window",
    action="bug.fields",
    change=None,
    pages=None,
    response=None,
):
    cfg, op = prepare(conn, setup, action=action, change=change)
    cfg.raw["project_integration"]["reader"] = dict(READER) | {
        "sha256": ACCEPTED_SHA256 if accepted else "a" * 64
    }
    cfg.raw["project_integration"]["field_writer"] = {
        "enabled": True,
        "user_key": "dedicated-user",
        "allowed_fields": ["progress"],
        "risk_policy": risk_policy,
        "closing_status_ids": ["CLOSED"],
    }
    native = WriteFake(
        pages if pages is not None else happy_pages(), response=response
    )
    native.subject = "dedicated-user"
    native.identity_checks = 0
    original = native.read_page

    def read(command, params):
        if command == "user.me":
            native.identity_checks += 1
            return {
                "host": native.host,
                "command": command,
                "payload": {"user_key": native.subject},
            }
        return original(command, params)

    native.read_page = read
    args = {
        "operation_id": op["operation_id"],
        "expected_digest": op["request_digest"],
        "request_id": "send-one",
    }
    return cfg, op, args, native


def test_write_queue_confirms_once_and_replays_idempotently(conn, setup, monkeypatch):
    cfg, _op, args, native = ready(conn, setup, monkeypatch)
    queued = execute(conn, cfg, action="send-write", payload=args)
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert (
        result["state"] == "succeeded"
        and result["result"]["operation_state"] == "confirmed"
    )
    assert len(native.updates) == 1 and native.identity_checks == 2
    assert (
        execute(conn, cfg, action="send-write", payload=args)["dispatch_id"]
        == queued["dispatch_id"]
    )
    assert (
        dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)["state"]
        == "idle"
    )
    status = execute(
        conn,
        cfg,
        action="write-send-status",
        payload={"dispatch_id": queued["dispatch_id"]},
    )
    assert status["state"] == "succeeded"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE project_write_dispatch_requests SET request_digest='x'")


def test_unaccepted_native_contract_blocks_enqueue_and_controls(
    conn, setup, monkeypatch
):
    cfg, op, args, native = ready(conn, setup, monkeypatch, accepted=False)
    control = dispatch.controls(
        conn, cfg, actor="owner", operation_id=op["operation_id"]
    )
    assert not control["available"]
    assert control["reason"] == "native_contract_unverified"
    with pytest.raises(dispatch.DispatchBlocked, match="native_contract_unverified"):
        execute(conn, cfg, action="send-write", payload=args)
    assert native.updates == []
    assert (
        conn.execute(
            "SELECT count(*) FROM project_write_dispatch_requests"
        ).fetchone()[0]
        == 0
    )


def test_forbid_risk_policy_blocks_the_queue_even_with_contracts(
    conn, setup, monkeypatch
):
    cfg, _op, args, native = ready(conn, setup, monkeypatch, risk_policy="forbid")
    with pytest.raises(dispatch.DispatchBlocked, match="risk_policy"):
        execute(conn, cfg, action="send-write", payload=args)
    assert native.updates == []


def test_transition_dispatch_requires_its_own_accepted_contract(
    conn, setup, monkeypatch
):
    cfg, _op, args, native = ready(
        conn,
        setup,
        monkeypatch,
        accepted=False,
        action="bug.transition",
        change={"transition_id": "to-test", "target_status_id": "testing"},
    )
    monkeypatch.setattr(policy, "ACCEPTED_UPDATE_CLIENTS", frozenset({"a" * 64}))
    with pytest.raises(dispatch.DispatchBlocked, match="native_contract_unverified"):
        execute(conn, cfg, action="send-write", payload=args)
    assert native.transitions == []


def test_grant_revocation_after_enqueue_prevents_the_write(conn, setup, monkeypatch):
    cfg, op, args, native = ready(conn, setup, monkeypatch)
    execute(conn, cfg, action="send-write", payload=args)
    grants.revoke(conn, actor="owner", grant_id=op["grant_id"])
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert result["state"] == "blocked" and native.updates == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_wrong_dedicated_identity_blocks_before_dispatch(conn, setup, monkeypatch):
    cfg, op, args, native = ready(conn, setup, monkeypatch)
    execute(conn, cfg, action="send-write", payload=args)
    native.subject = "other-user"
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert result["error_code"] == "writer_identity_changed" and native.updates == []
    assert operations._operation(conn, op["operation_id"])["state"] == "prepared"


def test_unknown_outcome_is_settled_by_recorded_human_check_via_controls(
    conn, setup, monkeypatch
):
    pages = snapshot_pages() + [transition_page(transition_row()), {}] + snapshot_pages() + snapshot_pages()
    cfg, op, args, native = ready(
        conn, setup, monkeypatch, pages=pages, response=TimeoutError("private")
    )
    execute(conn, cfg, action="send-write", payload=args)
    result = dispatch.run_one(conn, lambda: cfg, client_factory=lambda _: native)
    assert (
        result["state"] == "succeeded"
        and result["result"]["operation_state"] == "unknown"
    )
    assert result["result"]["reconcile_required"] and len(native.updates) == 1
    execute(
        conn,
        cfg,
        action="settle-unknown-write",
        payload={
            "operation_id": op["operation_id"],
            "verdict": "confirmed_not_applied",
            "evidence_text": "checked the item manually; no change was applied",
        },
    )
    assert operations._operation(conn, op["operation_id"])["state"] == "rejected"


def test_detail_projection_offers_the_write_send_control(conn, setup, monkeypatch):
    cfg, op, _args, _native = ready(conn, setup, monkeypatch)
    detail = execute(conn, cfg, action="detail", payload={"bug_id": op["bug_id"]})
    item = next(
        entry
        for entry in detail["operations"]
        if entry["operation_id"] == op["operation_id"]
    )
    assert item["send_control"]["available"] is True
    assert item["can_settle"] is False
    assert detail["close_approvals"] == []


def test_create_draft_flow_is_exposed_through_controls(conn, setup):
    from datetime import timedelta

    from k3_support.timeutil import utc_now

    cfg, _request, _adapter = setup
    grant = execute(
        conn,
        cfg,
        action="issue-create-grant",
        payload={
            "request_id": "cg-1",
            "scope": {
                "host": "project.feishu.cn",
                "project_key": "space",
                "type_key": "type",
                "max_creations": 1,
            },
            "expires_at": (utc_now() + timedelta(hours=1)).isoformat(),
        },
    )
    assert grant["status"] == "active" and grant["remaining"] == 1
    draft = execute(
        conn,
        cfg,
        action="prepare-create-draft",
        payload={
            "request_id": "cd-1",
            "grant_id": grant["grant_id"],
            "host": "project.feishu.cn",
            "project_key": "space",
            "type_key": "type",
            "field_values": {"name": "New defect"},
            "required_fields": [{"field_key": "name", "label": "Title"}],
        },
    )
    assert draft["state"] == "draft" and draft["missing_required"] == []
    with pytest.raises((ValueError, PermissionError)):
        execute(conn, cfg, action="attach-create-duplicates", payload={
            "draft_id": draft["draft_id"], "search_id": "invented", "candidates": [],
        })
    with pytest.raises(ValueError, match="duplicate"):
        execute(conn, cfg, action="mark-create-ready", payload={
            "draft_id": draft["draft_id"], "expected_digest": draft["request_digest"],
        })
    listing = execute(conn, cfg, action="create-drafts", payload={"after_id": ""})
    assert [item["draft_id"] for item in listing["items"]] == [draft["draft_id"]]
    grants_listing = execute(
        conn, cfg, action="list-create-grants", payload={"after_id": ""}
    )
    assert grants_listing["items"][0]["grant_id"] == grant["grant_id"]
    cancelled = execute(
        conn,
        cfg,
        action="cancel-create-draft",
        payload={
            "draft_id": draft["draft_id"],
            "expected_digest": draft["request_digest"],
        },
    )
    assert cancelled["state"] == "cancelled"
