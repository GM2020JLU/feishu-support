from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from reviewed_reply_fixture import attach_reviewed_reply

from k3_support.config import Config, validate_config
from k3_support.control import ControlMessage, execute_case_callback, execute_control
from k3_support.coordination import (
    bind_ai_communication,
    control_communication,
    ensure_turn,
    record_operator_message,
    record_operator_reaction,
)
from k3_support.db import transaction
from k3_support.delivery import DeliverySuppressed, claim_outbox, deliver_claimed
from k3_support.ingress import poll_operator_activity
from k3_support.lark import CommandResult
from k3_support.store import create_case, enqueue_outbox, ingest_event, transition_case


def active_config(config: Config) -> Config:
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"]["auto_faq"] = True
    raw["features"]["codex"] = True
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    raw["coordination"]["work_hours_send_grace_seconds"] = 0
    raw["coordination"]["off_hours_send_grace_seconds"] = 0
    return Config(validate_config(raw), config.path)


def make_turn(conn, *, chat_type="p2p", chat_id="oc_chat", message_id="om_question"):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id=message_id,
        payload={
            "content": "K3 启动失败",
            "chat_type": chat_type,
            "message_type": "text",
        },
        occurred_at="2026-09-02T02:00:00+00:00",
        sender_id="ou_colleague",
        chat_id=chat_id,
    )
    case_id, _ = create_case(
        conn,
        title="K3 启动失败",
        case_type="bug",
        severity="P2",
        confidence=0.8,
        requester_id="ou_colleague",
        requester_chat_id=chat_id,
        source_event_pk=event_pk,
    )
    with transaction(conn):
        turn = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
    return case_id, event_pk, turn


def queue_reply(conn, cfg, case_id, event_pk, message_id="om_question"):
    with transaction(conn):
        binding = bind_ai_communication(
            conn,
            cfg,
            case_id=case_id,
            source_event_pk=event_pk,
            at=datetime(2026, 9, 2, 2, 1, tzinfo=UTC),
        )
        assert binding is not None
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination=message_id,
            payload={"text": "[AI 自动回复] 测试", "identity": "user"},
            idempotency_key=f"reply:{case_id}:{binding['turn_revision']}",
            case_id=case_id,
            source_event_pk=event_pk,
            **binding,
        )
    attach_reviewed_reply(conn, outbox_id)
    return outbox_id


def test_direct_reply_preempts_only_communication_and_preserves_job(conn, config):
    cfg = active_config(config)
    case_id, event_pk, turn = make_turn(conn)
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
               VALUES('job-debug',?,'codex','queued','debug',?,?,?)""",
            (case_id, now, now, now),
        )
    result = record_operator_message(
        conn,
        external_id="om_owner_reply",
        actor_id="ou_owner",
        chat_id="oc_chat",
        occurred_at="2026-09-02T02:01:05+00:00",
        content="我来答这个问题",
        message_id="om_owner_reply",
        chat_type="p2p",
        reply_to_message_id="om_question",
    )
    assert result["signal"] == "hard"
    assert result["action"] == "human_answered"
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()[0]
        == "cancelled"
    )
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id='job-debug'").fetchone()[0]
        == "queued"
    )
    stored = conn.execute(
        "SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
    ).fetchone()
    assert (stored["communication_owner"], stored["state"]) == (
        "human",
        "human_answered",
    )


def test_outbox_respects_configured_grace_before_claim(conn, config):
    cfg = active_config(config)
    cfg.raw["coordination"]["work_hours_send_grace_seconds"] = 60
    cfg.raw["coordination"]["off_hours_send_grace_seconds"] = 60
    case_id, event_pk, _ = make_turn(conn)
    with transaction(conn):
        binding = bind_ai_communication(
            conn,
            cfg,
            case_id=case_id,
            source_event_pk=event_pk,
            at=datetime.now(UTC),
        )
        enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_question",
            payload={"text": "[AI 自动回复] 测试", "identity": "user"},
            idempotency_key="grace-reply",
            case_id=case_id,
            source_event_pk=event_pk,
            **binding,
        )
    assert claim_outbox(conn, worker_id="sender") is None
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE idempotency_key='grace-reply'"
        ).fetchone()[0]
        == "pending"
    )


def test_unthreaded_group_owner_message_is_soft_hold_not_false_reply_binding(
    conn, config
):
    cfg = active_config(config)
    case_id, event_pk, turn = make_turn(conn, chat_type="group", chat_id="oc_group")
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    result = record_operator_message(
        conn,
        external_id="om_owner_group",
        actor_id="ou_owner",
        chat_id="oc_group",
        occurred_at="2026-09-02T02:01:05+00:00",
        content="我先看一下",
        message_id="om_owner_group",
        chat_type="group",
    )
    assert (result["signal"], result["action"]) == ("soft", "claim")
    assert (
        conn.execute(
            "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()[0]
        == "cancelled"
    )
    assert (
        conn.execute(
            "SELECT state FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()[0]
        == "human_hold"
    )


def test_onit_reaction_claim_and_explicit_delegate_require_new_fence(conn, config):
    cfg = active_config(config)
    case_id, event_pk, _turn = make_turn(conn)
    old_outbox = queue_reply(conn, cfg, case_id, event_pk)
    claimed = record_operator_reaction(
        conn,
        reaction_id="reaction-1",
        actor_id="ou_owner",
        source_message_id="om_question",
        emoji_type="OnIt",
        occurred_at="2026-09-02T02:01:10+00:00",
    )
    assert claimed["created"] is True
    control = control_communication(
        conn,
        case_id=case_id,
        action="delegate",
        actor_id="owner-user",
        external_id="telegram:delegate-1",
    )
    assert control["turn"]["communication_owner"] == "ai"
    new_outbox = queue_reply(conn, cfg, case_id, event_pk)
    assert new_outbox != old_outbox
    rows = conn.execute(
        "SELECT state,communication_fence FROM outbox ORDER BY created_at"
    ).fetchall()
    assert rows[0]["state"] == "cancelled"
    assert rows[1]["communication_fence"] > rows[0]["communication_fence"]


def test_delivery_preflight_observes_owner_reply_and_never_calls_send(conn, config):
    cfg = active_config(config)
    case_id, event_pk, _ = make_turn(conn)
    outbox_id = queue_reply(conn, cfg, case_id, event_pk)
    row = claim_outbox(conn, worker_id="sender")
    calls = []

    def runner(argv):
        calls.append(argv)
        if "+chat-messages-list" in argv:
            return CommandResult(
                {
                    "messages": [
                        {
                            "message_id": "om_owner_reply",
                            "chat_id": "oc_chat",
                            "chat_type": "p2p",
                            "create_time": "1788314470000",
                            "sender": {"id": "ou_owner", "sender_type": "user"},
                            "msg_type": "text",
                            "content": "我来回复",
                            "parent_id": "om_question",
                        }
                    ],
                    "has_more": False,
                },
                "user",
                [],
            )
        pytest.fail("external send must be fenced after owner activity")

    with pytest.raises(DeliverySuppressed):
        deliver_claimed(conn, cfg, row, lark_runner=runner)
    stored = conn.execute(
        "SELECT state,suppression_reason FROM outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()
    assert stored["state"] == "cancelled"
    assert stored["suppression_reason"] == "context_revision_changed"
    assert sum("+chat-messages-list" in call for call in calls) == 1


def test_poll_operator_activity_reads_only_open_chat_and_detects_reaction(conn, config):
    cfg = active_config(config)
    _, _, turn = make_turn(conn)
    calls = []

    def runner(argv):
        calls.append(argv)
        return CommandResult(
            {
                "messages": [
                    {
                        "message_id": "om_question",
                        "chat_id": "oc_chat",
                        "chat_type": "p2p",
                        "create_time": "1788314400000",
                        "sender": {"id": "ou_colleague", "sender_type": "user"},
                        "msg_type": "text",
                        "content": "K3 启动失败",
                        "reactions": {
                            "details": [
                                {
                                    "reaction_id": "reaction-open",
                                    "operator": {
                                        "operator_id": "ou_owner",
                                        "operator_type": "user",
                                    },
                                    "action_time": "1788314470000",
                                    "emoji_type": "OnIt",
                                }
                            ]
                        },
                    }
                ],
                "has_more": False,
            },
            "user",
            [],
        )

    result = poll_operator_activity(
        conn, cfg, now=datetime(2026, 9, 2, 2, 2, tzinfo=UTC), runner=runner
    )
    assert result == {"state": "ready", "recorded": 1, "chats": 1}
    assert calls[0][0:4] == ["im", "+chat-messages-list", "--chat-id", "oc_chat"]
    assert (
        conn.execute(
            "SELECT state FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()[0]
        == "human_hold"
    )


def test_new_inbound_turn_supersedes_old_draft_and_uses_latest_reply_target(
    conn, config
):
    cfg = active_config(config)
    case_id, first_event, first_turn = make_turn(conn)
    first_outbox = queue_reply(conn, cfg, case_id, first_event)
    second_event, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_followup",
        payload={"content": "补充：只在 UFS 启动失败", "chat_type": "p2p"},
        occurred_at="2026-09-02T02:02:00+00:00",
        sender_id="ou_colleague",
        chat_id="oc_chat",
    )
    with transaction(conn):
        second_turn = ensure_turn(conn, case_id=case_id, source_event_pk=second_event)
    assert second_turn["source_message_id"] == "om_followup"
    assert (
        conn.execute(
            "SELECT state FROM conversation_turns WHERE turn_id=?",
            (first_turn["turn_id"],),
        ).fetchone()[0]
        == "closed"
    )
    stored = conn.execute(
        "SELECT state,suppression_reason FROM outbox WHERE outbox_id=?", (first_outbox,)
    ).fetchone()
    assert tuple(stored) == ("cancelled", "conversation_context_changed")


def test_case_button_is_bound_to_prompt_and_claim_does_not_take_over_case(conn, config):
    case_id, _, turn = make_turn(conn)
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={
                "case_id": case_id,
                "control_turn_id": turn["turn_id"],
                "control_fence": turn["fence"],
                "text": "choose",
                "buttons": [{"text": "我来回复", "callback_data": f"k3c:c:{case_id}"}],
            },
            idempotency_key="case-control-prompt",
            case_id=case_id,
        )
        conn.execute(
            "UPDATE outbox SET state='delivered',remote_message_id='prompt-case' WHERE outbox_id=?",
            (outbox_id,),
        )
    message = ControlMessage("owner-user", "owner-chat", "callback-case", "button")
    with pytest.raises(Exception, match="not bound"):
        execute_case_callback(
            conn,
            config,
            message,
            action="claim",
            case_id=case_id,
            prompt_message_id="forwarded",
        )
    with pytest.raises(Exception, match="not bound"):
        execute_case_callback(
            conn,
            config,
            message,
            action="takeover",
            case_id=case_id,
            prompt_message_id="prompt-case",
        )
    result = execute_case_callback(
        conn,
        config,
        message,
        action="claim",
        case_id=case_id,
        prompt_message_id="prompt-case",
    )
    assert result["turn"]["communication_owner"] == "human"
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "intake"
    )
    assert (
        conn.execute(
            "SELECT state FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()[0]
        == "human_hold"
    )


def test_whole_case_takeover_cancels_queue_and_expires_board_for_brom_cleanup(
    conn, config
):
    case_id, _, turn = make_turn(conn)
    now = datetime.now(UTC).isoformat()
    with transaction(conn):
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,created_at,updated_at)
               VALUES('job-takeover',?,'codex','queued','takeover',?,?,?)""",
            (case_id, now, now, now),
        )
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,
                   lease_owner,lease_expires_at,created_at,updated_at)
               VALUES('job-running-retrieve',?,'retrieve','running','running-retrieve',?,
                   'retriever','2099-01-01T00:00:00+00:00',?,?)""",
            (case_id, now, now, now),
        )
        conn.execute(
            """INSERT INTO jobs(job_id,case_id,job_type,state,input_digest,available_at,
                   lease_owner,lease_expires_at,created_at,updated_at)
               VALUES('job-running-codex',?,'codex','running','running-codex',?,
                   'codex-worker','2099-01-01T00:00:00+00:00',?,?)""",
            (case_id, now, now, now),
        )
        conn.execute(
            """INSERT INTO locks(lock_key,owner,case_id,scope,acquired_at,expires_at,heartbeat_at,metadata_json)
               VALUES('board1',?,?,'board1_session',?,'2099-01-01T00:00:00+00:00',?,'{}')""",
            (f"{case_id}:session-1", case_id, now, now),
        )
    result = execute_control(
        conn,
        config,
        ControlMessage(
            "owner-user", "owner-chat", "tg-takeover", f"takeover {case_id} 1"
        ),
    )
    assert result["state"] == "takeover"
    assert (
        conn.execute("SELECT state FROM jobs WHERE job_id='job-takeover'").fetchone()[0]
        == "cancelled"
    )
    assert {
        tuple(row)
        for row in conn.execute(
            """SELECT job_id,state FROM jobs
                 WHERE job_id IN ('job-running-retrieve','job-running-codex')"""
        )
    } == {
        ("job-running-retrieve", "cancelled"),
        ("job-running-codex", "cancelled"),
    }
    assert (
        conn.execute(
            "SELECT communication_owner FROM conversation_turns WHERE turn_id=?",
            (turn["turn_id"],),
        ).fetchone()[0]
        == "human"
    )
    lock = conn.execute(
        "SELECT expires_at,heartbeat_at FROM locks WHERE lock_key='board1'"
    ).fetchone()
    assert lock["expires_at"] == lock["heartbeat_at"]
    # Expiry requests BROM cleanup; it does not make the physical board reusable.
    from k3_support.approvals import (
        ApprovalError,
        decide_approval,
        expiry_after,
        normalized_board_action,
        request_approval,
    )

    approval_id, fingerprint, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="different-session",
        action=normalized_board_action(case_id, "different-session", 15),
        expires_at=expiry_after(15),
    )
    with pytest.raises(ApprovalError, match="cleanup pending"):
        decide_approval(
            conn,
            config,
            approval_id=approval_id,
            approve=True,
            approver_user_id="owner-user",
            approver_chat_id="owner-chat",
            message_id="tg-new-board",
            decision_text="approve",
            expected_digest=fingerprint,
        )


def test_delegate_button_continues_owner_decision_into_retrieval(conn, config):
    cfg = active_config(config)
    cfg.raw["features"]["codex"] = True
    case_id, _, turn = make_turn(conn)
    transition_case(
        conn,
        case_id=case_id,
        after="escalated",
        actor_type="system",
        actor_id=None,
        reason="owner decision",
        expected_version=1,
    )
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={
                "case_id": case_id,
                "control_turn_id": turn["turn_id"],
                "control_fence": turn["fence"],
                "text": "choose",
                "buttons": [{"text": "交给 AI", "callback_data": f"k3c:a:{case_id}"}],
            },
            idempotency_key="delegate-control-prompt",
            case_id=case_id,
        )
        conn.execute(
            "UPDATE outbox SET state='delivered',remote_message_id='prompt-delegate' WHERE outbox_id=?",
            (outbox_id,),
        )
    result = execute_case_callback(
        conn,
        cfg,
        ControlMessage("owner-user", "owner-chat", "callback-delegate", "button"),
        action="delegate",
        case_id=case_id,
        prompt_message_id="prompt-delegate",
    )
    assert result["continuation"]["route"] == "codex_debug"
    assert (
        conn.execute("SELECT state FROM cases WHERE case_id=?", (case_id,)).fetchone()[
            0
        ]
        == "investigating"
    )
    assert conn.execute(
        "SELECT job_type,state FROM jobs WHERE case_id=?", (case_id,)
    ).fetchone()[:] == ("retrieve", "queued")


def test_case_button_cannot_override_newer_human_claim(conn, config):
    case_id, _, turn = make_turn(conn)
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={
                "case_id": case_id,
                "control_turn_id": turn["turn_id"],
                "control_fence": turn["fence"],
                "text": "choose",
                "buttons": [{"text": "交给 AI", "callback_data": f"k3c:a:{case_id}"}],
            },
            idempotency_key="stale-control-prompt",
            case_id=case_id,
        )
        conn.execute(
            "UPDATE outbox SET state='delivered',remote_message_id='prompt-stale' WHERE outbox_id=?",
            (outbox_id,),
        )
    control_communication(
        conn,
        case_id=case_id,
        action="claim",
        actor_id="owner-user",
        external_id="telegram:newer-claim",
    )
    with pytest.raises(Exception, match="stale"):
        execute_case_callback(
            conn,
            config,
            ControlMessage("owner-user", "owner-chat", "callback-stale", "button"),
            action="delegate",
            case_id=case_id,
            prompt_message_id="prompt-stale",
        )


def test_old_case_card_conservatively_claims_latest_turn_in_same_chat(conn, config):
    case_id, _, first_turn = make_turn(conn)
    with transaction(conn):
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="telegram",
            action_type="owner_decision",
            destination="telegram:owner-chat",
            payload={
                "case_id": case_id,
                "control_turn_id": first_turn["turn_id"],
                "control_fence": first_turn["fence"],
                "text": "choose",
                "buttons": [{"text": "我来回复", "callback_data": f"k3c:c:{case_id}"}],
            },
            idempotency_key="retarget-control-prompt",
            case_id=case_id,
        )
        conn.execute(
            "UPDATE outbox SET state='delivered',remote_message_id='prompt-retarget' WHERE outbox_id=?",
            (outbox_id,),
        )
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_followup",
        payload={"content": "补充信息", "chat_type": "p2p", "message_type": "text"},
        occurred_at="2026-09-02T02:01:00+00:00",
        sender_id="ou_colleague",
        chat_id="oc_chat",
    )
    latest_turn = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)

    result = execute_case_callback(
        conn,
        config,
        ControlMessage("owner-user", "owner-chat", "callback-retarget", "button"),
        action="claim",
        case_id=case_id,
        prompt_message_id="prompt-retarget",
    )

    assert result["retargeted_from_turn_id"] == first_turn["turn_id"]
    assert result["retargeted_to_turn_id"] == latest_turn["turn_id"]
    assert result["turn"]["communication_owner"] == "human"
    assert result["turn"]["state"] == "human_hold"
