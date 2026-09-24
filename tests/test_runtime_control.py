from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from k3_support.config import Config, validate_config
from k3_support.control import ControlMessage, execute_control
from k3_support.coordination import (
    bind_ai_communication,
    control_communication,
    ensure_turn,
)
from k3_support.db import transaction
from k3_support.delivery import DeliverySuppressed, claim_outbox, deliver_claimed
from k3_support.runtime_control import (
    RuntimeControlError,
    bind_global_panel,
    capability_allowed,
    current_global_state,
    execute_global_callback,
)
from k3_support.store import create_case, enqueue_outbox, ingest_event, transition_case


def active_config(config: Config) -> Config:
    raw = copy.deepcopy(config.raw)
    raw["mode"] = "active"
    raw["features"].update(
        {"auto_faq": True, "codex": True, "board": True, "wip_push": True}
    )
    raw["identity"]["feishu_owner_open_id"] = "ou_owner"
    raw["coordination"]["work_hours_send_grace_seconds"] = 0
    raw["coordination"]["off_hours_send_grace_seconds"] = 0
    return Config(validate_config(raw), config.path)


def issue_and_bind(conn, cfg, *, command_id="tg-command", prompt_id="tg-panel"):
    payload = execute_control(
        conn,
        cfg,
        ControlMessage("owner-user", "owner-chat", command_id, "/feishu"),
    )
    bind_global_panel(
        conn,
        panel_id=payload["panel_id"],
        operator_user_id="owner-user",
        chat_id="owner-chat",
        command_message_id=command_id,
        prompt_message_id=prompt_id,
    )
    return payload["panel_id"]


def click(conn, cfg, panel_id, action, *, callback="callback-1", prompt="tg-panel"):
    return execute_global_callback(
        conn,
        cfg,
        action=action,
        panel_id=panel_id,
        operator_user_id="owner-user",
        chat_id="owner-chat",
        callback_query_id=callback,
        prompt_message_id=prompt,
    )


def make_turn(conn):
    event_pk, _ = ingest_event(
        conn,
        source="feishu_user_poll",
        identity="user",
        external_id="om_runtime_question",
        payload={"content": "为什么启动失败", "chat_type": "p2p"},
        occurred_at="2026-09-02T02:00:00+00:00",
        sender_id="ou_colleague",
        chat_id="oc_chat",
    )
    case_id, _ = create_case(
        conn,
        title="启动失败",
        case_type="bug",
        severity="P2",
        confidence=0.8,
        requester_id="ou_colleague",
        requester_chat_id="oc_chat",
        source_event_pk=event_pk,
    )
    with transaction(conn):
        turn = ensure_turn(conn, case_id=case_id, source_event_pk=event_pk)
    return case_id, event_pk, turn


def test_feishu_is_only_global_command_and_initializes_observe(conn, config):
    cfg = active_config(config)
    with pytest.raises(Exception, match="unsupported"):
        execute_control(
            conn,
            cfg,
            ControlMessage("owner-user", "owner-chat", "old", "/k3"),
        )
    panel_id = issue_and_bind(conn, cfg)
    state = current_global_state(conn, cfg)
    assert state["mode"] == "observe"
    assert state["revision"] == 1
    assert capability_allowed(conn, cfg, "triage") is True
    assert capability_allowed(conn, cfg, "codex") is False
    assert panel_id.startswith("gcp_")

    panel = execute_control(
        conn,
        cfg,
        ControlMessage("owner-user", "owner-chat", "tg-panel-read", "/feishu"),
    )
    assert "待你判断：0" in panel["text"]
    assert "AI 处理中：0" in panel["text"]
    assert "观察记录：0" in panel["text"]
    assert "已启用：知识回复、Codex、board1、WIP push" in panel["text"]


def test_panel_is_bound_to_exact_chat_message_and_new_panel_retires_old(conn, config):
    cfg = active_config(config)
    first = issue_and_bind(conn, cfg)
    with pytest.raises(RuntimeControlError, match="identity"):
        click(conn, cfg, first, "global_collaborate", prompt="forwarded")
    second = issue_and_bind(
        conn, cfg, command_id="tg-command-2", prompt_id="tg-panel-2"
    )
    with pytest.raises(RuntimeControlError, match="stale"):
        click(conn, cfg, first, "global_collaborate", callback="old-panel")
    result = click(
        conn,
        cfg,
        second,
        "global_collaborate",
        callback="new-panel",
        prompt="tg-panel-2",
    )
    assert result["mode"] == "collaborate"


def test_auto_requires_confirmation_and_auto60_expires_to_collaborate(conn, config):
    cfg = active_config(config)
    panel = issue_and_bind(conn, cfg)
    requested = click(conn, cfg, panel, "global_auto_request")
    assert requested["mode"] == "observe"
    assert "再次确认" in requested["text"]
    confirmed = click(
        conn, cfg, panel, "global_auto_confirm", callback="callback-confirm"
    )
    assert confirmed["mode"] == "auto"
    timed = click(conn, cfg, panel, "global_auto_60", callback="callback-timed")
    assert timed["mode"] == "auto_60"
    assert any(button["text"] == "✅ 自动60分钟" for button in timed["buttons"])
    conn.execute(
        "UPDATE global_control_state SET auto_expires_at=? WHERE scope='feishu_support'",
        ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),),
    )
    expired = current_global_state(conn, cfg)
    assert expired["mode"] == "collaborate"
    assert expired["revision"] == timed["revision"] + 1


def test_collaborate_holds_reply_until_explicit_case_delegate(conn, config):
    cfg = active_config(config)
    panel = issue_and_bind(conn, cfg)
    click(conn, cfg, panel, "global_collaborate")
    case_id, event_pk, turn = make_turn(conn)
    with transaction(conn):
        binding = bind_ai_communication(
            conn, cfg, case_id=case_id, source_event_pk=event_pk
        )
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_runtime_question",
            payload={"text": "[AI 自动回复] 测试", "identity": "user"},
            idempotency_key="collaborate-reply",
            case_id=case_id,
            source_event_pk=event_pk,
            **binding,
        )
    assert (
        capability_allowed(conn, cfg, "public_reply", turn_id=turn["turn_id"]) is False
    )
    assert (
        claim_outbox(
            conn,
            worker_id="sender",
            eligible=lambda row: capability_allowed(
                conn, cfg, "public_reply", turn_id=row.get("turn_id")
            ),
        )
        is None
    )
    control_communication(
        conn,
        case_id=case_id,
        action="delegate",
        actor_id="owner-user",
        external_id="telegram:delegate-runtime",
    )
    assert (
        capability_allowed(conn, cfg, "public_reply", turn_id=turn["turn_id"]) is True
    )
    assert claim_outbox(conn, worker_id="sender")["outbox_id"] == outbox_id


def test_pause_fences_claimed_reply_and_stop_requires_observe_restart(conn, config):
    cfg = active_config(config)
    panel = issue_and_bind(conn, cfg)
    click(conn, cfg, panel, "global_auto_request")
    click(conn, cfg, panel, "global_auto_confirm", callback="auto-confirm")
    case_id, event_pk, _ = make_turn(conn)
    with transaction(conn):
        binding = bind_ai_communication(
            conn, cfg, case_id=case_id, source_event_pk=event_pk
        )
        enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_runtime_question",
            payload={"text": "[AI 自动回复] 测试", "identity": "user"},
            idempotency_key="fenced-reply",
            case_id=case_id,
            source_event_pk=event_pk,
            **binding,
        )
    row = claim_outbox(conn, worker_id="sender")
    click(conn, cfg, panel, "global_pause", callback="pause")
    with pytest.raises(DeliverySuppressed):
        deliver_claimed(
            conn, cfg, row, lark_runner=lambda *_: pytest.fail("must not send")
        )

    click(conn, cfg, panel, "global_stop_request", callback="stop-request")
    stopped = click(conn, cfg, panel, "global_stop_confirm", callback="stop-confirm")
    assert stopped["mode"] == "stopped"
    with pytest.raises(RuntimeControlError, match="必须先切换到观察"):
        click(conn, cfg, panel, "global_collaborate", callback="bad-restart")
    restarted = click(conn, cfg, panel, "global_observe", callback="restart")
    assert restarted["mode"] == "observe"


def test_global_fence_moves_cancelled_reply_out_of_sending_states(conn, config):
    cfg = active_config(config)
    panel = issue_and_bind(conn, cfg)
    click(conn, cfg, panel, "global_auto_request")
    click(conn, cfg, panel, "global_auto_confirm", callback="auto-confirm")
    case_id, event_pk, turn = make_turn(conn)
    transition_case(
        conn,
        case_id=case_id,
        after="triage",
        actor_type="system",
        actor_id="test",
        reason="classified",
        expected_version=1,
    )
    transition_case(
        conn,
        case_id=case_id,
        after="answering",
        actor_type="system",
        actor_id="test",
        reason="draft ready",
        expected_version=2,
    )
    with transaction(conn):
        binding = bind_ai_communication(
            conn, cfg, case_id=case_id, source_event_pk=event_pk
        )
        outbox_id, _ = enqueue_outbox(
            conn,
            channel="feishu_im",
            action_type="reply",
            destination="om_runtime_question",
            payload={"text": "[AI 自动回复] 测试", "identity": "user"},
            idempotency_key="mode-fence-reply",
            case_id=case_id,
            source_event_pk=event_pk,
            **binding,
        )

    click(conn, cfg, panel, "global_pause", callback="pause-after-draft")

    assert conn.execute(
        "SELECT state FROM outbox WHERE outbox_id=?", (outbox_id,)
    ).fetchone()[0] == "cancelled"
    assert tuple(conn.execute(
        "SELECT state,communication_owner FROM conversation_turns WHERE turn_id=?",
        (turn["turn_id"],),
    ).fetchone()) == ("human_hold", "human")
    assert tuple(conn.execute(
        "SELECT state,next_action FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()) == (
        "investigating",
        "Global mode changed; reply requires fresh review or delegation",
    )
