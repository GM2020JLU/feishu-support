from __future__ import annotations

import copy

import pytest
from reviewed_reply_fixture import enqueue_bound_reviewed_reply

from k3_support.approvals import expiry_after, normalized_board_action, request_approval
from k3_support.calendar import create_meeting_preview, normalize_meeting_action
from k3_support.config import Config, validate_config
from k3_support.control import (
    ControlError,
    ControlMessage,
    execute_approval_callback,
    execute_control,
)
from k3_support.conversation_context import admit_im_event
from k3_support.db import transaction
from k3_support.delivery import (
    DeliveryError,
    DeliveryReceipt,
    DeliveryUncertain,
    claim_outbox,
    deliver_claimed,
    enqueue_p0_alert,
)
from k3_support.knowledge import create_candidate
from k3_support.lark import CommandResult
from k3_support.runtime_control import ensure_global_state, outbox_eligible
from k3_support.store import create_case, enqueue_outbox, transition_case
from k3_support.timeutil import iso_now


def active_config(config, **features):
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"].update(features)
    if raw["features"]["auto_faq"]:
        raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    return Config(validate_config(raw), config.path)


def test_control_rejects_forged_identity_and_is_idempotent(conn, config):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id=None,
        reason="ready",
        expected_version=1,
    )
    forged = ControlMessage("fake", "owner-chat", "tg-1", f"pause {case_id} 2")
    with pytest.raises(Exception, match="identity mismatch"):
        execute_control(conn, config, forged)
    message = ControlMessage(
        "owner-user", "owner-chat", "tg-2", f"pause {case_id} 2 debugging"
    )
    first = execute_control(conn, config, message)
    second = execute_control(conn, config, message)
    assert first["state"] == "paused"
    assert second["version"] == first["version"]


def test_control_approval_requires_exact_gate_and_digest(conn, config):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    action = normalized_board_action(case_id, "s1", 30)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="s1",
        action=action,
        expires_at=expiry_after(30),
    )
    with pytest.raises(ControlError):
        execute_control(
            conn,
            config,
            ControlMessage(
                "owner-user",
                "owner-chat",
                "tg-a",
                f"approve push {approval_id} {action_digest}",
            ),
        )
    result = execute_control(
        conn,
        config,
        ControlMessage(
            "owner-user",
            "owner-chat",
            "tg-b",
            f"approve board {approval_id} {action_digest}",
        ),
    )
    assert result["approval"]["status"] == "approved"


def test_button_callback_is_bound_to_exact_delivered_prompt(conn, config):
    case_id, _ = create_case(
        conn, title="x", case_type="bug", severity="P2", confidence=0.2
    )
    action = normalized_board_action(case_id, "s1", 30)
    approval_id, _, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="s1",
        action=action,
        expires_at=expiry_after(30),
    )
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="approval_request",
            destination="telegram:owner-chat",
            payload={"text": "approve", "approval_id": approval_id},
            idempotency_key="button-prompt",
            case_id=case_id,
        )
        conn.execute(
            "UPDATE outbox SET state='delivered',remote_message_id='prompt-7' WHERE outbox_id=?",
            (outbox_id,),
        )
    message = ControlMessage("owner-user", "owner-chat", "callback-1", "button")
    with pytest.raises(ControlError, match="not bound"):
        execute_approval_callback(
            conn,
            config,
            message,
            action="approve",
            approval_id=approval_id,
            prompt_message_id="forwarded-copy",
        )
    details = execute_approval_callback(
        conn,
        config,
        message,
        action="details",
        approval_id=approval_id,
        prompt_message_id="prompt-7",
    )
    assert details["approval"]["status"] == "requested"
    approved = execute_approval_callback(
        conn,
        config,
        ControlMessage("owner-user", "owner-chat", "callback-2", "button"),
        action="approve",
        approval_id=approval_id,
        prompt_message_id="prompt-7",
    )
    assert approved["approval"]["status"] == "approved"


def test_approval_outbox_uses_button_sender(conn, config):
    cfg = active_config(config)
    with transaction(conn):
        enqueue_outbox(
            conn,
            channel="telegram",
            action_type="approval_request",
            destination="telegram:owner-chat",
            payload={
                "text": "approve?",
                "approval_id": "apr_test",
                "buttons": [{"text": "yes", "callback_data": "k3a:a:apr_test"}],
            },
            idempotency_key="button-delivery",
        )
    row = claim_outbox(conn, worker_id="sender")
    calls = []

    def send_buttons(target, text, buttons):
        calls.append((target, text, buttons))
        return DeliveryReceipt("prompt-9", {"ok": True, "message_id": "prompt-9"})

    receipt = deliver_claimed(
        conn,
        cfg,
        row,
        telegram_runner=lambda *_: pytest.fail("text sender must not be used"),
        telegram_button_runner=send_buttons,
    )
    assert receipt.remote_id == "prompt-9"
    assert calls[0][2][0]["callback_data"] == "k3a:a:apr_test"


def test_control_meeting_approval_executes_only_exact_preview(
    conn, config, monkeypatch
):
    cfg = active_config(config, calendar=True)
    case_id, _ = create_case(
        conn, title="meeting", case_type="meeting", severity="P3", confidence=0.8
    )
    preview = create_meeting_preview(
        conn,
        action=normalize_meeting_action(
            case_id=case_id,
            summary="K3 review",
            start="2027-01-05T14:00:00+08:00",
            end="2027-01-05T14:30:00+08:00",
            attendee_ids=["ou_colleague"],
            description="review",
        ),
    )
    calls = []

    def execute(conn, config, *, preview_id, runner):
        calls.append((preview_id, runner))
        return {"event_id": "event_1", "preview_id": preview_id}

    monkeypatch.setattr("k3_support.calendar.execute_meeting_create", execute)
    result = execute_control(
        conn,
        cfg,
        ControlMessage(
            "owner-user",
            "owner-chat",
            "tg-meeting-approve",
            f"approve meeting {preview['approval_id']} {preview['action_digest']}",
        ),
    )
    assert result["approval"]["status"] == "approved"
    assert result["execution"]["event_id"] == "event_1"
    assert [call[0] for call in calls] == [preview["preview_id"]]


def test_control_identity_can_review_knowledge_by_stable_id(conn, config):
    knowledge_id = create_candidate(
        conn,
        title="K3 FAQ",
        questions=["how"],
        answer_markdown="answer",
        project="k3",
        module="boot",
        software_version=None,
        disclosure_class="internal",
        confidence=0.9,
        source_authority=0.9,
        canonical_case_id=None,
        source_digest="source",
    )
    shown = execute_control(
        conn,
        config,
        ControlMessage(
            "owner-user", "owner-chat", "tg-k1", f"knowledge show {knowledge_id}"
        ),
    )
    assert shown["knowledge"]["status"] == "candidate"
    approved = execute_control(
        conn,
        config,
        ControlMessage(
            "owner-user", "owner-chat", "tg-k2", f"knowledge approve {knowledge_id}"
        ),
    )
    assert approved["knowledge"]["status"] == "approved"
    assert approved["knowledge"]["reviewed_by"] == "owner-user"
    with pytest.raises(Exception, match="identity mismatch"):
        execute_control(
            conn,
            config,
            ControlMessage(
                "forged", "owner-chat", "tg-k3", f"knowledge retire {knowledge_id}"
            ),
        )


def test_shadow_delivery_fails_closed_without_sending(conn, config):
    with transaction(conn):
        enqueue_outbox(
            conn,
            channel="telegram",
            action_type="notify",
            destination="telegram:owner-chat",
            payload={"text": "test"},
            idempotency_key="shadow-send",
        )
    row = claim_outbox(conn, worker_id="sender")
    calls = []
    with pytest.raises(DeliveryError, match="disabled"):
        deliver_claimed(
            conn,
            config,
            row,
            telegram_runner=lambda target, text: calls.append((target, text)),
        )
    assert calls == []
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE idempotency_key='shadow-send'"
        ).fetchone()[0]
        == "pending"
    )


def test_active_reply_uses_recorded_identity_and_marks_receipt(conn, config):
    cfg = active_config(config, auto_faq=True, codex=True)
    source_event_pk, admitted = admit_im_event(
        conn,
        cfg,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_123",
            "payload": {
                "content": "Synthetic reviewed reply request",
                "chat_type": "p2p",
            },
            "occurred_at": iso_now(),
            "sender_id": "ou_fixture",
            "chat_id": "oc_fixture",
        },
    )
    assert admitted
    case_id, _ = create_case(
        conn,
        title="faq",
        case_type="faq",
        severity="P3",
        confidence=0.95,
        requester_id="ou_fixture",
        requester_chat_id="oc_fixture",
        source_event_pk=source_event_pk,
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
    transition_case(
        conn,
        case_id=case_id,
        after="answering",
        actor_type="system",
        actor_id=None,
        reason="approved knowledge",
        expected_version=2,
    )
    conn.execute(
        "UPDATE cases SET active_job_id='job_old',active_session_id='session_old',next_action='old action' WHERE case_id=?",
        (case_id,),
    )
    outbox_id = enqueue_bound_reviewed_reply(
        conn,
        cfg,
        source_event_pk=source_event_pk,
        destination="om_123",
        payload={
            "text": "[AI 自动回复]\n\n**答复**\n\nanswer",
            "identity": "user",
            "format": "markdown",
        },
        idempotency_key="reply-1",
        case_id=case_id,
    )
    row = claim_outbox(conn, worker_id="sender")
    calls = []

    def runner(argv):
        calls.append(argv)
        if "--markdown" not in argv:
            return CommandResult({"messages": [], "has_more": False}, "user", [])
        return CommandResult({"message_id": "om_reply"}, "user", [])

    receipt = deliver_claimed(conn, cfg, row, lark_runner=runner)
    assert receipt.remote_id == "om_reply"
    writes = [argv for argv in calls if "--markdown" in argv]
    assert len(writes) == 1
    assert "--text" not in writes[0]
    assert writes[0][-2:] == ["--as", "user"]
    stored = conn.execute(
        "SELECT state,remote_message_id FROM outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    assert tuple(stored) == ("delivered", "om_reply")
    case = conn.execute(
        "SELECT state,active_job_id,active_session_id,next_action FROM cases WHERE case_id=?",
        (case_id,),
    ).fetchone()
    # A current FAQ receipt records an answer, not a verified incident fix;
    # independent execution ownership must still remain untouched.
    assert tuple(case) == (
        "monitoring",
        "job_old",
        "session_old",
        "答复已送达；不自动追问或要求确认。",
    )
    assert tuple(
        conn.execute(
            "SELECT outcome,resolved_at FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
    ) == ("answered", None)


def test_reply_without_remote_receipt_is_terminal_and_does_not_resolve_case(
    conn, config
):
    cfg = active_config(config, auto_faq=True, codex=True)
    source_event_pk, admitted = admit_im_event(
        conn,
        cfg,
        {
            "source": "feishu_user_poll",
            "identity": "user",
            "external_id": "om_no_receipt",
            "payload": {
                "content": "Synthetic reviewed reply request",
                "chat_type": "p2p",
            },
            "occurred_at": iso_now(),
            "sender_id": "ou_fixture",
            "chat_id": "oc_fixture",
        },
    )
    assert admitted
    case_id, _ = create_case(
        conn,
        title="faq",
        case_type="faq",
        severity="P3",
        confidence=0.95,
        requester_id="ou_fixture",
        requester_chat_id="oc_fixture",
        source_event_pk=source_event_pk,
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
    transition_case(
        conn,
        case_id=case_id,
        after="answering",
        actor_type="system",
        actor_id=None,
        reason="approved knowledge",
        expected_version=2,
    )
    outbox_id = enqueue_bound_reviewed_reply(
        conn,
        cfg,
        source_event_pk=source_event_pk,
        destination="om_no_receipt",
        payload={"text": "[AI 自动回复]answer", "identity": "user"},
        idempotency_key="reply-no-receipt",
        case_id=case_id,
    )
    row = claim_outbox(conn, worker_id="sender")

    with pytest.raises(DeliveryUncertain, match="no remote message ID"):
        deliver_claimed(
            conn,
            cfg,
            row,
            lark_runner=lambda _: CommandResult({}, "user", []),
        )

    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()[0]
        == "permanent_failure"
    )
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "answering"
    )


def test_telegram_without_remote_receipt_is_not_automatically_retried(conn, config):
    cfg = active_config(config)
    with transaction(conn):
        enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={"text": "need decision"},
            idempotency_key="telegram-no-receipt",
        )
    row = claim_outbox(conn, worker_id="sender")

    with pytest.raises(DeliveryUncertain, match="no remote message ID"):
        deliver_claimed(
            conn,
            cfg,
            row,
            telegram_runner=lambda *_: DeliveryReceipt(None, {"ok": True}),
        )

    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE idempotency_key='telegram-no-receipt'"
        ).fetchone()[0]
        == "permanent_failure"
    )


def test_observe_delivers_owner_mail_summary_but_pause_holds_it(conn, config):
    cfg = active_config(config, auto_faq=False)
    ensure_global_state(
        conn,
        actor_id="test",
        source="init_db",
        external_id="test-observe-state",
    )
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="mail_summary",
            destination="telegram:owner-chat",
            payload={"text": "summary"},
            idempotency_key="observe-mail-summary",
        )
    row = dict(
        conn.execute("SELECT * FROM outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
    )

    assert outbox_eligible(conn, cfg, row) is True
    conn.execute(
        "UPDATE global_control_state SET mode='paused' WHERE scope='feishu_support'"
    )
    assert outbox_eligible(conn, cfg, row) is False


def test_mail_alert_marks_item_notified_only_after_telegram_receipt(conn, config):
    cfg = active_config(config, auto_faq=False)
    ensure_global_state(
        conn,
        actor_id="test",
        source="init_db",
        external_id="test-mail-alert-state",
    )
    case_id, _ = create_case(
        conn, title="mail", case_type="mail", severity="P1", confidence=0.9
    )
    conn.execute(
        """INSERT INTO mail_items(message_id,subject,classification,notified,case_id,received_at,updated_at)
           VALUES('mail-alert','urgent','urgent_action',0,?,'2026-09-03T00:00:00+00:00','2026-09-03T00:00:00+00:00')""",
        (case_id,),
    )
    with transaction(conn):
        _outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="incident_alert",
            destination="telegram:owner-chat",
            payload={"text": "urgent mail"},
            idempotency_key="mail-alert-receipt",
            case_id=case_id,
        )
    row = claim_outbox(
        conn,
        worker_id="sender",
        eligible=lambda value: outbox_eligible(conn, cfg, value),
    )
    assert row is not None
    assert (
        conn.execute(
            "SELECT notified FROM mail_items WHERE message_id='mail-alert'"
        ).fetchone()[0]
        == 0
    )

    deliver_claimed(
        conn,
        cfg,
        row,
        telegram_runner=lambda target, text: DeliveryReceipt(
            "tg-mail-alert", {"ok": True, "message_id": "tg-mail-alert"}
        ),
    )

    assert (
        conn.execute(
            "SELECT notified FROM mail_items WHERE message_id='mail-alert'"
        ).fetchone()[0]
        == 1
    )


def test_active_ack_does_not_require_auto_faq_feature(conn, config):
    cfg = active_config(config, auto_faq=False)
    with transaction(conn):
        enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="ack",
            destination="om_123",
            payload={"text": "[AI 助手处理中] case", "identity": "user"},
            idempotency_key="ack-1",
        )
    row = claim_outbox(conn, worker_id="sender")
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult({"message_id": "om_ack"}, "user", [])

    receipt = deliver_claimed(
        conn,
        cfg,
        row,
        lark_runner=runner,
    )
    assert receipt.remote_id == "om_ack"
    assert "--text" in calls[0]
    assert "--markdown" not in calls[0]


def test_p0_bot_receipt_does_not_create_disabled_urgent_actions(conn, config):
    cfg = active_config(config)
    cfg.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    cfg.raw["identity"]["feishu_p0_chat_id"] = "oc_incident"
    case_id, _ = create_case(
        conn, title="outage", case_type="incident", severity="P0", confidence=0.9
    )
    enqueue_p0_alert(
        conn,
        cfg,
        case_id=case_id,
        revision=1,
        text="K3 P0 严重故障提醒\nCase: K3-1\n标题: 启动失败 [待确认]",
    )
    telegram = claim_outbox(conn, worker_id="sender")
    assert telegram["channel"] == "telegram"
    deliver_claimed(
        conn,
        cfg,
        telegram,
        telegram_runner=lambda target, text: DeliveryReceipt("tg-1", {"id": "tg-1"}),
    )
    bot = claim_outbox(conn, worker_id="sender")
    assert bot["channel"] == "feishu_im"
    calls = []

    def lark_runner(argv):
        calls.append(argv)
        return CommandResult({"message_id": "om_alert"}, "bot", [])

    deliver_claimed(
        conn,
        cfg,
        bot,
        lark_runner=lark_runner,
    )
    assert "--markdown" in calls[0]
    markdown = calls[0][calls[0].index("--markdown") + 1]
    assert markdown.startswith("### K3 P0 严重故障提醒")
    assert "**Case：** K3-1" in markdown
    assert r"启动失败 \[待确认\]" in markdown
    urgent = conn.execute(
        "SELECT channel,destination,state FROM outbox WHERE channel LIKE 'feishu_urgent_%' ORDER BY channel"
    ).fetchall()
    assert [tuple(row) for row in urgent] == []


def test_p0_bot_receipt_creates_only_explicitly_enabled_urgent_actions(conn, config):
    cfg = active_config(config)
    cfg.raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    cfg.raw["identity"]["feishu_p0_chat_id"] = "oc_incident"
    cfg.raw["notifications"]["feishu_app_urgent"] = True
    case_id, _ = create_case(
        conn, title="outage", case_type="incident", severity="P0", confidence=0.9
    )
    enqueue_p0_alert(conn, cfg, case_id=case_id, revision=1, text="P0 test")
    telegram = claim_outbox(conn, worker_id="sender")
    deliver_claimed(
        conn,
        cfg,
        telegram,
        telegram_runner=lambda target, text: DeliveryReceipt("tg-1", {"id": "tg-1"}),
    )
    bot = claim_outbox(conn, worker_id="sender")
    deliver_claimed(
        conn,
        cfg,
        bot,
        lark_runner=lambda argv: CommandResult({"message_id": "om_alert"}, "bot", []),
    )
    urgent = conn.execute(
        "SELECT channel,destination,state FROM outbox WHERE channel LIKE 'feishu_urgent_%' ORDER BY channel"
    ).fetchall()
    assert [tuple(row) for row in urgent] == [
        ("feishu_urgent_app", "om_alert", "pending"),
    ]
