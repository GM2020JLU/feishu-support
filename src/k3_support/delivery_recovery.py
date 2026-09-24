"""Durable, fenced communication handoff after a content-evidence rejection.

This does not cancel jobs, revoke approvals, change Case execution ownership,
release board leases, or assert that a human has actually replied.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

from .coordination import validate_outbox_fence
from .ids import canonical_json, new_id
from .notification_schedule import window
from .operator_notifications import destination as notice_destination
from .operator_notifications import enqueue as enqueue_notice
from .runtime_control import capability_allowed
from .timeutil import epoch_now


def _now():
    return datetime.now(UTC)


def _notification_time(config, now):
    return window(config, now)["until_at"]


def _notice(conn, config, block):
    """The row is the persistent notification intent if identity is unavailable."""
    if (
        block["notification_outbox_id"]
        or not notice_destination(config)
        or config.mode != "active"
        or not capability_allowed(conn, config, "operator_prompt")
    ):
        return
    is_clarification = block["reason"].startswith("clarification_review:")
    label = (
        (
            "追问已送达，但依据随后变化，需要复核。"
            if block["was_delivered"]
            else "这条追问的依据已变化，尚未向同事发送，也不会等待对方回答。"
        )
        if is_clarification
        else (
            "答复已送达，但知识资格随后失效，需要复核。"
            if block["was_delivered"]
            else "知识校验拦住了这次答复，尚未向同事发送。"
        )
    )
    oid, _ = enqueue_notice(
        conn, config,
        action_type="owner_decision",
        case_id=block["case_id"],
        idempotency_key=f"delivery-block:{block['outbox_id']}:notice",
        not_before=_notification_time(config, _now()),
        payload={
            "case_id": block["case_id"],
            "control_turn_id": block["turn_id"],
            "control_fence": block["communication_fence"],
            "delivery_block_outbox_id": block["outbox_id"],
            "parse_mode": "HTML",
            "text": (
                "<b>这条答复需要你处理</b>\n"
                f"<code>{html.escape(block['case_id'])}</code>\n\n{label}\n"
                "已获准的调试或板卡会话不受影响；继续回答前须重新查证。"
            ),
            "buttons": [
                {
                    "text": "我来回复",
                    "callback_data": f"k3c:c:{block['case_id']}",
                    "row": 0,
                },
                {
                    "text": "查看详情",
                    "callback_data": f"k3c:i:{block['case_id']}",
                    "row": 0,
                },
            ],
        },
    )
    conn.execute(
        "UPDATE delivery_blocks SET notification_outbox_id=? WHERE outbox_id=?",
        (oid, block["outbox_id"]),
    )


def record_blocked_delivery(conn, config, row, *, reason: str) -> bool:
    """Called in the same write transaction as suppression/finalization."""
    if not conn.in_transaction:
        raise RuntimeError("delivery handoff requires the caller's transaction")
    is_clarification = (
        reason.startswith("clarification_review:")
        and row.get("action_type") == "clarify"
    )
    is_knowledge = (
        reason.startswith("knowledge_release:") and row.get("action_type") == "reply"
    )
    if row.get("channel") != "feishu_im" or not (is_knowledge or is_clarification):
        return False
    if not row.get("case_id") or not row.get("turn_id"):
        return False
    if config.mode != "active" or not capability_allowed(
        conn, config, "operator_prompt"
    ):
        return False
    saved = conn.execute(
        "SELECT * FROM outbox WHERE outbox_id=?", (row["outbox_id"],)
    ).fetchone()
    if saved is None or saved["claim_token"] != row.get("claim_token"):
        return False
    saved = dict(saved)
    if saved["state"] not in {"cancelled", "delivered"}:
        return False
    if conn.execute(
        "SELECT 1 FROM delivery_blocks WHERE outbox_id=?", (row["outbox_id"],)
    ).fetchone():
        return False
    case = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (saved["case_id"],)
    ).fetchone()
    if (
        case is None
        or case["lifecycle_round"] != saved["lifecycle_round"]
        or case["state"] in {"resolved", "takeover", "cancelled", "paused"}
    ):
        return False
    valid, _ = validate_outbox_fence(conn, saved)
    if not valid:
        return False
    turn = conn.execute(
        "SELECT * FROM conversation_turns WHERE turn_id=?", (saved["turn_id"],)
    ).fetchone()
    delivered = saved["state"] == "delivered" and bool(saved["remote_message_id"])
    if saved["state"] == "delivered" and not delivered:
        return False
    now = _now().isoformat()
    next_action = (
        (
            "追问已送达，但依据随后变化；请人工复核，不自动补发。"
            if delivered
            else "追问依据变化，尚未发送；待你处理，不等待同事回答，不自动补问。"
        )
        if is_clarification
        else (
            "答复已送达，但知识资格随后失效；请人工复核已发送内容，不自动重发。"
            if delivered
            else "知识资格阻断，待你处理这次沟通；尚未发送，须重新查证后再生成答复。"
        )
    )
    conn.execute(
        """UPDATE conversation_turns SET communication_owner='human',communication_mode='silent',
           state='human_hold',fence=fence+1,revision=revision+1,updated_at=?
           WHERE turn_id=? AND revision=? AND fence=?""",
        (now, turn["turn_id"], turn["revision"], turn["fence"]),
    )
    # Keep execution state, owner, jobs, approvals and physical locks intact.
    conn.execute(
        """UPDATE cases SET next_action=?,version=version+1,updated_at=?,updated_epoch=?
           WHERE case_id=? AND version=?""",
        (next_action, now, epoch_now(), case["case_id"], case["version"]),
    )
    conn.execute(
        """INSERT INTO delivery_blocks(outbox_id,case_id,lifecycle_round,claim_token,
           turn_id,turn_revision,communication_fence,reason,next_action,was_delivered,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            saved["outbox_id"],
            case["case_id"],
            case["lifecycle_round"],
            saved["claim_token"],
            turn["turn_id"],
            turn["revision"] + 1,
            turn["fence"] + 1,
            reason[:1000],
            next_action,
            int(delivered),
            now,
        ),
    )
    sequence = conn.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,actor_id,
           source_event_pk,before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
           VALUES(?,?,?,?,'system',?,?,?,?,?,?,?,?)""",
        (
            new_id("cev"),
            case["case_id"],
            sequence,
            "clarification_delivery_blocked"
            if is_clarification
            else "knowledge_delivery_blocked",
            "clarification-review" if is_clarification else "knowledge-release",
            saved["source_event_pk"],
            case["state"],
            case["state"],
            canonical_json(
                {
                    "outbox_id": saved["outbox_id"],
                    "reason": reason[:1000],
                    "was_delivered": bool(delivered),
                    "communication_owner": "human",
                    "execution_authority_unchanged": True,
                }
            ),
            f"delivery-block:{saved['outbox_id']}",
            now,
            epoch_now(),
        ),
    )
    block = conn.execute(
        "SELECT * FROM delivery_blocks WHERE outbox_id=?", (saved["outbox_id"],)
    ).fetchone()
    _notice(conn, config, block)
    return True


def unreconciled_blocked_deliveries(conn) -> list[dict]:
    """Only current content-evidence cancellations, not normal preemption."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT o.* FROM outbox o JOIN cases c ON c.case_id=o.case_id
           JOIN conversation_turns t ON t.turn_id=o.turn_id
           WHERE o.state='cancelled' AND o.channel='feishu_im'
             AND ((o.suppression_reason LIKE 'knowledge_release:%' AND o.action_type='reply')
                  OR (o.suppression_reason LIKE 'clarification_review:%' AND o.action_type='clarify'))
             AND c.lifecycle_round=o.lifecycle_round
             AND c.state NOT IN ('resolved','cancelled','takeover','paused')
             AND t.fence=o.communication_fence AND t.revision=o.turn_revision
             AND t.communication_owner='ai' AND t.communication_mode='respond'
             AND t.state IN ('ai_scheduled','ai_sending')
             AND NOT EXISTS(SELECT 1 FROM delivery_blocks b WHERE b.outbox_id=o.outbox_id)
           ORDER BY o.created_at,o.outbox_id"""
        )
        if validate_outbox_fence(conn, dict(row))[0]
    ]


def reconcile_delivery_blocks(conn, config) -> int:
    recovered = 0
    for row in unreconciled_blocked_deliveries(conn):
        recovered += int(
            record_blocked_delivery(conn, config, row, reason=row["suppression_reason"])
        )
    for block in conn.execute(
        "SELECT * FROM active_delivery_blocks WHERE notification_outbox_id IS NULL"
    ).fetchall():
        _notice(conn, config, block)
    return recovered
