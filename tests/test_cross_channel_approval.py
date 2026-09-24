import itertools

import pytest
from test_coordination import make_turn
from test_feishu_control import setup_identity

from k3_support.approvals import (
    ApprovalError,
    approval_binding,
    expiry_after,
    normalized_board_action,
    request_approval,
)
from k3_support.control import ControlMessage, _decide_and_continue
from k3_support.store import ConflictError

IDENTITIES = {
    "telegram": ("owner-user", "owner-chat"),
    "gui": ("shared-owner", "web"),
    "feishu": ("tenant-user", "oc_control"),
}


@pytest.mark.parametrize("first,second", list(itertools.permutations(IDENTITIES, 2)))
@pytest.mark.parametrize("approve", [True, False])
def test_cross_channel_receipt_preserves_audit_without_dispatch(
    conn, config, monkeypatch, first, second, approve
):
    setup_identity(config)
    case_id, _, _ = make_turn(conn)
    identifier, digest, _ = request_approval(
        conn,
        approval_type="board1_lease",
        case_id=case_id,
        session_id="cross-channel",
        action=normalized_board_action(case_id, "cross-channel", 15),
        expires_at=expiry_after(15),
    )

    def decide(channel, decision, **overrides):
        user, chat = IDENTITIES[channel]
        args = {
            "approval_id": identifier,
            "approve": decision,
            "expected_digest": digest,
            "expected_binding": approval_binding(conn, identifier),
            "control_channel": channel,
        }
        args.update(overrides)
        return _decide_and_continue(
            conn,
            config,
            ControlMessage(user, chat, channel + "-message", channel + "-decision"),
            **args,
        )

    decide(first, approve)
    before = dict(
        conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (identifier,)
        ).fetchone()
    )
    # Enable continuations only after the first decision: a duplicate must not
    # turn on execution that was disabled when the original decision committed.
    config.raw["features"].update(codex=True, board=True)
    monkeypatch.setattr("k3_support.control.capability_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(
        "k3_support.review.continue_after_board_approval",
        lambda *a, **kw: pytest.fail("duplicate dispatch"),
    )
    result = decide(second, approve)
    assert result["replayed"] and "continuation" not in result
    assert (
        dict(
            conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (identifier,)
            ).fetchone()
        )
        == before
    )
    with pytest.raises(ConflictError):
        decide(second, not approve)
    with pytest.raises(ApprovalError, match="digest mismatch"):
        decide(second, approve, expected_digest="wrong")
    with pytest.raises(ApprovalError, match="details changed"):
        decide(second, approve, expected_binding="old-preview")
