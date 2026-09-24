from uuid import uuid4

import pytest
from test_mail_actions import arguments
from test_mail_snapshot import item
from test_meeting_recovery import CalendarFixture, configured

from k3_support import mail_actions, mail_meeting_drafts, mail_meeting_prepare
from k3_support.approvals import ApprovalError, decide_approval
from k3_support.calendar import exact_meeting_preview
from k3_support.store import create_case


def setup(conn):
    message, _, _ = item(conn, 1)
    case, _ = create_case(
        conn, title="meeting", case_type="meeting", severity="P3", confidence=1
    )
    mail_actions.apply(conn, **arguments(conn, message, "link_case", case_id=case))
    draft = mail_meeting_drafts.read(conn, message)
    draft["draft"].update(
        start="2099-01-01T09:00:00+08:00",
        end="2099-01-01T10:00:00+08:00",
        attendee_ids=["ou_peer"],
    )
    saved = mail_meeting_drafts.save(
        conn,
        message_id=message,
        draft=draft["draft"],
        expected_revision=0,
        source_digest=draft["source_digest"],
        request_id=str(uuid4()),
        actor_id="owner",
    )
    args = {
        "message_id": message,
        "expected_revision": 1,
        "source_digest": saved["source_digest"],
        "request_id": str(uuid4()),
        "actor_id": "owner",
    }
    return message, args


def test_preparation_is_read_only_and_stale_source_blocks_approval(
    conn, config, monkeypatch
):
    message, args = setup(conn)
    cfg = configured(config)
    monkeypatch.setattr(mail_meeting_prepare, "capability_allowed", lambda *args: True)
    request = mail_meeting_prepare.enqueue(conn, **args)
    assert (
        mail_meeting_prepare.enqueue(conn, **args)["request_id"]
        == request["request_id"]
    )
    fixture = CalendarFixture()

    def reader(argv, **kwargs):
        if argv[:3] == ["calendar", "calendars", "primary"]:
            return fixture(argv, **kwargs)
        raise TimeoutError("synthetic busy query unavailable")

    result = mail_meeting_prepare.dispatch_one(conn, cfg, runner=reader)
    assert result["state"] == "prepared"
    _, action = exact_meeting_preview(conn, result["preview_id"])
    assert (
        action["schema_version"] == 2 and action["availability"]["state"] == "unknown"
    )
    assert all(
        call[:3] == ["calendar", "calendars", "primary"] for call in fixture.calls
    )
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    assert mail_meeting_prepare.dispatch_one(conn, cfg, runner=reader) is None
    conn.execute(
        "UPDATE mail_catalog_items SET subject='new' WHERE message_id=?", (message,)
    )
    with pytest.raises(ApprovalError, match="draft changed"):
        exact_meeting_preview(conn, result["preview_id"])
    approval = conn.execute("SELECT * FROM approvals").fetchone()
    with pytest.raises(ApprovalError, match="draft changed"):
        decide_approval(
            conn,
            cfg,
            approval_id=approval["approval_id"],
            approve=True,
            approver_user_id=cfg.telegram_control_user_id,
            approver_chat_id=cfg.telegram_control_chat_id,
            message_id="synthetic",
            decision_text="approve",
            expected_digest=approval["action_digest"],
        )


def test_shadow_failure_and_dispatched_never_reclaimed(conn, config, monkeypatch):
    _, args = setup(conn)
    mail_meeting_prepare.enqueue(conn, **args)

    def fail(*args, **kwargs):
        raise TimeoutError("private adapter details")

    assert mail_meeting_prepare.dispatch_one(conn, config, runner=fail) is None
    cfg = configured(config)
    monkeypatch.setattr(mail_meeting_prepare, "capability_allowed", lambda *args: True)
    result = mail_meeting_prepare.dispatch_one(conn, cfg, runner=fail)
    assert result["state"] == "needs_review" and "private" not in str(result)
    assert mail_meeting_prepare.dispatch_one(conn, cfg, runner=fail) is None


def test_cancel_fences_late_orphan_preview_and_allows_new_version(
    conn, config, monkeypatch
):
    from k3_support.ids import canonical_json
    from k3_support.mail_actions import _digest

    message, args = setup(conn)
    row = mail_meeting_prepare.enqueue(conn, **args)
    cfg = configured(config)
    monkeypatch.setattr(mail_meeting_prepare, "capability_allowed", lambda *args: True)
    current = mail_meeting_drafts.read(conn, message)
    cancellation = {
        "prepare_request_id": row["request_id"],
        "binding_digest": current["preparation"]["binding_digest"],
        "request_id": str(uuid4()),
        "actor_id": "owner",
    }
    original = mail_meeting_prepare.create_meeting_preview

    def late_create(conn, **kwargs):
        mail_meeting_prepare.cancel(conn, **cancellation)
        return original(conn, **kwargs)

    monkeypatch.setattr(mail_meeting_prepare, "create_meeting_preview", late_create)
    fixture = CalendarFixture()

    def reader(argv, **kwargs):
        if argv[:3] == ["calendar", "calendars", "primary"]:
            return fixture(argv, **kwargs)
        raise TimeoutError("synthetic")

    result = mail_meeting_prepare.dispatch_one(conn, cfg, runner=reader)
    assert result["state"] == "needs_review" and result["preview_id"]
    assert (
        mail_meeting_drafts.read(conn, message)["preparation"]["state"] == "cancelled"
    )
    with pytest.raises(ApprovalError):
        exact_meeting_preview(conn, result["preview_id"])
    assert mail_meeting_prepare.cancel(conn, **cancellation)["replayed"]
    with pytest.raises(ValueError, match="cancelled"):
        mail_meeting_prepare.enqueue(conn, **args)
    # Saving a new explicit version does not revive the cancelled request.
    refreshed = mail_meeting_drafts.read(conn, message)
    saved = mail_meeting_drafts.save(
        conn,
        message_id=message,
        draft=refreshed["draft"],
        expected_revision=1,
        source_digest=refreshed["source_digest"],
        request_id=str(uuid4()),
        actor_id="owner",
    )
    next_request = mail_meeting_prepare.enqueue(
        conn,
        **{**args, "expected_revision": saved["revision"], "request_id": str(uuid4())},
    )
    assert next_request["state"] == "queued"
    assert _digest(refreshed["draft"]) == _digest(saved["draft"])
    assert canonical_json(saved["draft"]) == canonical_json(refreshed["draft"])


def test_cancel_refuses_started_meeting_even_if_queue_lost_preview_link(
    conn, config, monkeypatch
):
    message, args = setup(conn)
    mail_meeting_prepare.enqueue(conn, **args)
    monkeypatch.setattr(mail_meeting_prepare, "capability_allowed", lambda *args: True)
    fixture = CalendarFixture()

    def reader(argv, **kwargs):
        if argv[:3] == ["calendar", "calendars", "primary"]:
            return fixture(argv, **kwargs)
        raise TimeoutError("synthetic")

    result = mail_meeting_prepare.dispatch_one(conn, configured(config), runner=reader)
    assert result["state"] == "prepared"
    conn.execute("UPDATE approvals SET consumed_at='2026-09-07T00:00:00+00:00'")
    conn.execute("UPDATE mail_meeting_prepare SET preview_id=NULL,state='dispatched'")
    preparation = mail_meeting_drafts.read(conn, message)["preparation"]
    with pytest.raises(ValueError, match="execution already began"):
        mail_meeting_prepare.cancel(
            conn,
            prepare_request_id=preparation["request_id"],
            binding_digest=preparation["binding_digest"],
            request_id=str(uuid4()),
            actor_id="owner",
        )
    conn.execute("UPDATE mail_meeting_prepare SET state='dispatched'")
    assert mail_meeting_prepare.dispatch_one(conn, configured(config), runner=reader) is None
