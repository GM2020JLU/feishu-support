from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from k3_support.approvals import (
    BOARD_SCOPE,
    ApprovalError,
    consume_push_approval,
    decide_approval,
    expiry_after,
    normalized_board_action,
    normalized_push_action,
    request_approval,
    valid_board_lease,
    valid_push_approval,
)
from k3_support.ids import digest
from k3_support.store import create_case
from k3_support.timeutil import parse_iso


def _case(conn):
    return create_case(
        conn, title="test", case_type="bug", severity="P2", confidence=0.3
    )[0]


def test_board_lease_has_blanket_scope_and_stable_identity(conn, config):
    case_id = _case(conn)
    action = normalized_board_action(case_id, "session-1", 45)
    assert action["scope"] == BOARD_SCOPE
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="session-1",
        action=action,
        expires_at=expiry_after(60),
    )
    try:
        decide_approval(
            conn,
            config,
            approval_id=approval_id,
            approve=True,
            approver_user_id="fake-owner",
            approver_chat_id="owner-chat",
            message_id="tg-1",
            decision_text="approve",
            expected_digest=action_digest,
        )
        assert False, "forged identity accepted"
    except ApprovalError:
        pass
    decide_approval(
        conn,
        config,
        approval_id=approval_id,
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-2",
        decision_text="批准 session-1 的完整 board1 占用范围",
        expected_digest=action_digest,
    )
    lease = valid_board_lease(conn, case_id=case_id, session_id="session-1")
    assert lease is not None
    remaining = parse_iso(lease["expires_at"]) - datetime.now(UTC)
    assert timedelta(minutes=44) < remaining <= timedelta(minutes=45)
    assert valid_board_lease(conn, case_id=case_id, session_id="other") is None


def test_push_approval_is_exact_and_one_time(conn, config):
    case_id = _case(conn)
    action = normalized_push_action(
        case_id=case_id,
        repo="u-boot",
        destination="refs/for/main%wip",
        commits=["a" * 40],
        command=["git", "push", "origin", "HEAD:refs/for/main%wip"],
    )
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="wip_push",
        case_id=case_id,
        action=action,
        expires_at=expiry_after(30),
    )
    decide_approval(
        conn,
        config,
        approval_id=approval_id,
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-push-1",
        decision_text=f"approve {action_digest}",
        expected_digest=action_digest,
    )
    assert valid_push_approval(conn, case_id=case_id, action=action) is not None
    changed = {**action, "commits": ["def456"]}
    assert valid_push_approval(conn, case_id=case_id, action=changed) is None
    consume_push_approval(conn, approval_id, expected_digest=digest(action))
    assert valid_push_approval(conn, case_id=case_id, action=action) is None
    try:
        consume_push_approval(conn, approval_id, expected_digest=digest(action))
        assert False, "consumed approval accepted twice"
    except ApprovalError:
        pass


def test_board1_global_lock_rejects_a_second_case_lease(conn, config):
    first_case = _case(conn)
    second_case = _case(conn)
    approvals = []
    for case_id in (first_case, second_case):
        session_id = f"session-{case_id}"
        action = normalized_board_action(case_id, session_id, 30)
        approval_id, action_digest, _ = request_approval(
            conn,
            approval_type="board1_lease",
            case_id=case_id,
            session_id=session_id,
            action=action,
            expires_at=expiry_after(30),
        )
        approvals.append((approval_id, action_digest))
    decide_approval(
        conn,
        config,
        approval_id=approvals[0][0],
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-lock-1",
        decision_text="approve first",
        expected_digest=approvals[0][1],
    )
    with pytest.raises(ApprovalError, match="already leased"):
        decide_approval(
            conn,
            config,
            approval_id=approvals[1][0],
            approve=True,
            approver_user_id="owner-user",
            approver_chat_id="owner-chat",
            message_id="tg-lock-2",
            decision_text="approve second",
            expected_digest=approvals[1][1],
        )


def test_exact_approval_retry_with_new_message_id_is_receipt_safe(conn, config):
    case_id = _case(conn)
    action = normalized_board_action(case_id, "session-receipt", 30)
    approval_id, action_digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="session-receipt",
        action=action,
        expires_at=expiry_after(30),
    )
    decision_text = f"approve board {approval_id} {action_digest}"
    first = decide_approval(
        conn,
        config,
        approval_id=approval_id,
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-first",
        decision_text=decision_text,
        expected_digest=action_digest,
    )
    replay = decide_approval(
        conn,
        config,
        approval_id=approval_id,
        approve=True,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-replay",
        decision_text=decision_text,
        expected_digest=action_digest,
    )
    assert replay["approval_message_id"] == "tg-first"
    assert replay["decided_at"] == first["decided_at"]
    with pytest.raises(Exception, match="already approved"):
        decide_approval(
            conn,
            config,
            approval_id=approval_id,
            approve=False,
            approver_user_id="owner-user",
            approver_chat_id="owner-chat",
            message_id="tg-changed",
            decision_text=f"deny {approval_id} {action_digest}",
            expected_digest=action_digest,
        )


def test_terminal_approval_does_not_block_a_new_exact_request(conn, config):
    case_id = _case(conn)
    action = normalized_push_action(
        case_id=case_id,
        repo="u-boot",
        destination="refs/for/main%wip",
        commits=["a" * 40],
        command=["git", "push", "origin", "HEAD:refs/for/main%wip"],
    )
    first_id, action_digest, created = request_approval(
        conn,
        approval_type="wip_push",
        case_id=case_id,
        action=action,
        expires_at=expiry_after(30),
    )
    assert created is True
    decide_approval(
        conn,
        config,
        approval_id=first_id,
        approve=False,
        approver_user_id="owner-user",
        approver_chat_id="owner-chat",
        message_id="tg-deny-first",
        decision_text="deny first exact request",
        expected_digest=action_digest,
    )

    second_id, second_digest, created = request_approval(
        conn,
        approval_type="wip_push",
        case_id=case_id,
        action=action,
        expires_at=expiry_after(30),
    )

    assert created is True
    assert second_id != first_id
    assert second_digest == action_digest
    assert (
        conn.execute(
            "SELECT status FROM approvals WHERE approval_id=?", (first_id,)
        ).fetchone()[0]
        == "denied"
    )


def test_due_exact_approval_is_expired_before_reissue(conn):
    case_id = _case(conn)
    action = {"case_id": case_id, "operation": "same"}
    first_id, action_digest, created = request_approval(
        conn,
        approval_type="policy_change",
        case_id=case_id,
        action=action,
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert created is True

    second_id, second_digest, created = request_approval(
        conn,
        approval_type="policy_change",
        case_id=case_id,
        action=action,
        expires_at=expiry_after(30),
    )

    assert created is True
    assert second_id != first_id
    assert second_digest == action_digest
    assert conn.execute(
        "SELECT status FROM approvals WHERE approval_id=?", (first_id,)
    ).fetchone()[0] == "expired"
    assert conn.execute(
        "SELECT status FROM approvals WHERE approval_id=?", (second_id,)
    ).fetchone()[0] == "requested"


def test_channel_identities_cannot_be_substituted(config):
    from k3_support.approvals import ApprovalError, verify_control_identity
    config.raw["identity"]["control_operator_id"] = "web-owner"
    verify_control_identity(config, "web-owner", "web", channel="gui")
    with pytest.raises(ApprovalError):
        verify_control_identity(config, "web-owner", "web", channel="telegram")
    with pytest.raises(ApprovalError):
        verify_control_identity(config, config.telegram_control_user_id, config.telegram_control_chat_id, channel="gui")
    config.raw["identity"].update(telegram_control_user_id=None, telegram_control_chat_id=None)
    with pytest.raises(ApprovalError):
        verify_control_identity(config, None, None)


@pytest.mark.parametrize("channel", ["telegram", "gui"])
def test_verified_channels_record_one_operator_identity(conn, config, channel):
    config.raw["identity"]["control_operator_id"] = "shared-owner"
    case_id = _case(conn)
    approval_id, action_digest, _ = request_approval(
        conn, approval_type="board1_lease", case_id=case_id, session_id="shared",
        action=normalized_board_action(case_id, "shared", 15), expires_at=expiry_after(15))
    from k3_support.approvals import approval_binding
    user, chat = ((config.telegram_control_user_id, config.telegram_control_chat_id)
                  if channel == "telegram" else ("shared-owner", "web"))
    result = decide_approval(conn, config, approval_id=approval_id, approve=False,
        approver_user_id=user, approver_chat_id=chat, message_id="channel-message",
        decision_text="deny exact action", expected_digest=action_digest,
        control_channel=channel, expected_binding=approval_binding(conn, approval_id))
    assert result["approver_identity"] == "shared-owner"
    assert result["approver_channel"] == channel
    assert result["approval_message_id"] == "channel-message"
    again = decide_approval(conn, config, approval_id=approval_id, approve=False,
        approver_user_id=user, approver_chat_id=chat, message_id="receipt-retry",
        decision_text="deny exact action", expected_digest=action_digest,
        control_channel=channel, expected_binding=approval_binding(conn, approval_id))
    assert again == result
