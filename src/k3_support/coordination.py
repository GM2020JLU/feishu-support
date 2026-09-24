from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .config import Config, is_work_time
from .conversation_context import (
    adopt_case_event,
    atomic,
    resolve_event_context,
    set_case_communication,
    validate_context_binding,
)
from .db import transaction
from .ids import canonical_json, new_id
from .timeutil import epoch_now, iso_now, utc_now


class CoordinationError(RuntimeError):
    pass


PUBLIC_ACTIONS = {"reply", "ack", "clarify"}
ACTIVE_TURN_STATES = {"open", "ai_scheduled", "ai_sending", "human_hold"}
AI_MARKERS = ("[AI 自动回复]", "[AI 助手处理中]", "[AI 助手确认]")


def _event_payload(event: sqlite3.Row) -> dict[str, Any]:
    value = json.loads(event["payload_json"])
    return value if isinstance(value, dict) else {}


def ensure_turn(
    conn: sqlite3.Connection, *, case_id: str, source_event_pk: str
) -> dict[str, Any] | None:
    event = conn.execute(
        "SELECT * FROM inbound_events WHERE event_pk=?", (source_event_pk,)
    ).fetchone()
    if event is None or event["source"] not in {"feishu_bot_im", "feishu_user_poll"}:
        return None
    payload = _event_payload(event)
    # Older imported events did not persist chat_type; a stable chat with one
    # requester is conservatively treated as P2P for migration compatibility.
    chat_type = str(payload.get("chat_type") or "p2p")
    if chat_type not in {"p2p", "group"} or not event["chat_id"]:
        return None
    context = adopt_case_event(conn, case_id, source_event_pk)
    now = iso_now()
    turn_id = new_id("trn")
    inserted = conn.execute(
        """INSERT OR IGNORE INTO conversation_turns(
               turn_id,case_id,source_event_pk,source_message_id,chat_id,chat_type,
               thread_id,root_message_id,reply_to_message_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            turn_id,
            case_id,
            source_event_pk,
            event["external_id"],
            event["chat_id"],
            chat_type,
            event["thread_id"],
            payload.get("root_id"),
            payload.get("reply_to") or payload.get("parent_id"),
            event["occurred_at"],
            now,
        ),
    ).rowcount
    row = conn.execute(
        "SELECT * FROM conversation_turns WHERE source_event_pk=?", (source_event_pk,)
    ).fetchone()
    case_round = conn.execute(
        "SELECT lifecycle_round,owner FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if (
        row is not None
        and inserted
        and (
            (context and context["communication_owner"] == "human")
            or (case_round and case_round["owner"] == "operator")
        )
    ):
        mode = context["communication_mode"] if context else "silent"
        conn.execute(
            "UPDATE conversation_turns SET communication_owner='human',communication_mode=?,state='human_hold' WHERE turn_id=?",
            (mode, row["turn_id"]),
        )
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id=?", (row["turn_id"],)
        ).fetchone()
    if row is not None and inserted:
        previous = conn.execute(
            """SELECT turn_id,created_at FROM conversation_turns WHERE case_id=? AND turn_id<>?
                 AND state IN ('open','ai_scheduled','ai_sending','human_hold')""",
            (case_id, row["turn_id"]),
        ).fetchall()
        newer_exists = any(old["created_at"] > row["created_at"] for old in previous)
        superseded = (
            [row]
            if newer_exists
            else [old for old in previous if old["created_at"] <= row["created_at"]]
        )
        for old in superseded:
            conn.execute(
                """UPDATE conversation_turns SET state='closed',fence=fence+1,updated_at=?
                   WHERE turn_id=?""",
                (now, old["turn_id"]),
            )
            _cancel_stale_public_outbox(
                conn,
                turn_id=str(old["turn_id"]),
                reason="superseded_by_new_inbound_turn",
            )
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE source_event_pk=?",
            (source_event_pk,),
        ).fetchone()
    return dict(row) if row else None


def communication_grace_seconds(config: Config, at: datetime | None = None) -> int:
    in_hours = is_work_time(config, at)
    key = (
        "work_hours_send_grace_seconds" if in_hours else "off_hours_send_grace_seconds"
    )
    return int(config.raw["coordination"][key])


def bind_ai_communication(
    conn: sqlite3.Connection,
    config: Config,
    *,
    case_id: str,
    source_event_pk: str,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Reserve communication authority and return an immutable Outbox fence."""
    turn = ensure_turn(conn, case_id=case_id, source_event_pk=source_event_pk)
    if turn is None:
        return None
    if turn["communication_owner"] != "ai" or turn["communication_mode"] != "respond":
        return None
    if turn["state"] in {"human_hold", "human_answered", "ai_sent", "closed"}:
        return None
    context = resolve_event_context(conn, source_event_pk)
    if (
        context
        and not validate_context_binding(conn, context["binding"], require_ai=True)[0]
    ):
        return None
    observed = at or utc_now()
    not_before = (
        observed + timedelta(seconds=communication_grace_seconds(config, observed))
    ).isoformat()
    conn.execute(
        "UPDATE conversation_turns SET state='ai_scheduled',updated_at=? WHERE turn_id=?",
        (iso_now(), turn["turn_id"]),
    )
    return {
        "turn_id": str(turn["turn_id"]),
        "turn_revision": int(turn["revision"]),
        "communication_fence": int(turn["fence"]),
        "not_before": not_before,
        **(context["binding"] if context else {}),
    }


def _cancel_stale_public_outbox(
    conn: sqlite3.Connection, *, turn_id: str, reason: str
) -> int:
    cursor = conn.execute(
        """UPDATE outbox SET state='cancelled',suppression_reason=?,lease_owner=NULL,
                  lease_expires_at=NULL,updated_at=?
             WHERE turn_id=? AND channel='feishu_im' AND action_type IN ('reply','ack','clarify')
               AND state IN ('pending','retry')""",
        (reason, iso_now(), turn_id),
    )
    return int(cursor.rowcount)


def _record_case_event(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    actor_id: str,
    event_type: str,
    detail: dict[str, Any],
    idempotency_key: str,
) -> None:
    if conn.execute(
        "SELECT 1 FROM case_events WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone():
        return
    case = conn.execute(
        "SELECT state FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None:
        return
    sequence = conn.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
        (case_id,),
    ).fetchone()[0]
    now = iso_now()
    conn.execute(
        """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,actor_id,
               before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
           VALUES(?,?,?,?,'operator',?,?,?,?,?,?,?)""",
        (
            new_id("cev"),
            case_id,
            sequence,
            event_type,
            actor_id,
            case["state"],
            case["state"],
            canonical_json(detail),
            idempotency_key,
            now,
            epoch_now(),
        ),
    )


def _apply_human_claim(
    conn: sqlite3.Connection,
    *,
    turn: sqlite3.Row,
    activity_type: str,
    signal: str,
    action: str,
    external_id: str,
    actor_id: str,
    occurred_at: str,
    message_id: str | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    root_message_id: str | None = None,
    reply_to_message_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    activity_id = new_id("opa")
    inserted = conn.execute(
        """INSERT OR IGNORE INTO operator_activities(
               activity_id,external_id,activity_type,signal,action,message_id,chat_id,thread_id,
               root_message_id,reply_to_message_id,matched_turn_id,actor_id,occurred_at,
               detail_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            activity_id,
            external_id,
            activity_type,
            signal,
            action,
            message_id,
            chat_id,
            thread_id,
            root_message_id,
            reply_to_message_id,
            turn["turn_id"],
            actor_id,
            occurred_at,
            canonical_json(detail or {}),
            iso_now(),
        ),
    ).rowcount
    if not inserted:
        return False
    state = "human_answered" if action == "human_answered" else "human_hold"
    conn.execute(
        """UPDATE conversation_turns SET communication_owner='human',communication_mode='silent',
               state=?,fence=fence+1,human_activity_at=?,claimed_at=coalesce(claimed_at,?),updated_at=?
           WHERE turn_id=?""",
        (state, occurred_at, occurred_at, iso_now(), turn["turn_id"]),
    )
    cancelled = _cancel_stale_public_outbox(
        conn, turn_id=str(turn["turn_id"]), reason=f"human_{signal}_{action}"
    )
    set_case_communication(
        conn, str(turn["case_id"]), owner="human", mode="silent", reason=external_id
    )
    _record_case_event(
        conn,
        case_id=str(turn["case_id"]),
        actor_id=actor_id,
        event_type="communication_preempted",
        detail={
            "turn_id": turn["turn_id"],
            "signal": signal,
            "action": action,
            "activity_type": activity_type,
            "cancelled_outbox": cancelled,
            "investigation_continues": True,
        },
        idempotency_key=f"operator:{external_id}",
    )
    return True


def record_operator_message(
    conn: sqlite3.Connection,
    *,
    external_id: str,
    actor_id: str,
    chat_id: str,
    occurred_at: str,
    content: str,
    message_id: str,
    chat_type: str,
    thread_id: str | None = None,
    root_message_id: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    if content.startswith(AI_MARKERS):
        return {"matched": False, "reason": "ai_marker"}
    rows = conn.execute(
        """SELECT ct.* FROM conversation_turns ct
             JOIN inbound_events ie ON ie.event_pk=ct.source_event_pk
             WHERE ct.chat_id=? AND ct.state IN ('open','ai_scheduled','ai_sending','human_hold')
               AND ie.occurred_at<?
             ORDER BY ct.created_at DESC""",
        (chat_id, occurred_at),
    ).fetchall()
    if not rows:
        return {"matched": False, "reason": "no_active_turn"}
    explicit_ids = {
        value for value in (reply_to_message_id, root_message_id, thread_id) if value
    }
    hard = next(
        (
            row
            for row in rows
            if explicit_ids.intersection(
                value
                for value in (
                    row["source_message_id"],
                    row["root_message_id"],
                    row["thread_id"],
                )
                if value
            )
        ),
        None,
    )
    turn = hard or rows[0]
    signal = "hard" if hard is not None else "soft"
    action = "human_answered" if hard is not None else "claim"
    with atomic(conn):
        created = _apply_human_claim(
            conn,
            turn=turn,
            activity_type="message",
            signal=signal,
            action=action,
            external_id=f"feishu-message:{external_id}",
            actor_id=actor_id,
            occurred_at=occurred_at,
            message_id=message_id,
            chat_id=chat_id,
            thread_id=thread_id,
            root_message_id=root_message_id,
            reply_to_message_id=reply_to_message_id,
            detail={
                "chat_type": chat_type,
                "content_preview": " ".join(content.split())[:160],
            },
        )
        # An unthreaded group message is intentionally not guessed onto one
        # question. Freeze every open Turn in that chat until the operator
        # explicitly delegates the intended Case.
        if signal == "soft" and chat_type == "group" and created:
            for other in rows[1:]:
                conn.execute(
                    """UPDATE conversation_turns SET communication_owner='human',
                           communication_mode='silent',state='human_hold',fence=fence+1,
                           human_activity_at=?,claimed_at=coalesce(claimed_at,?),updated_at=?
                       WHERE turn_id=?""",
                    (occurred_at, occurred_at, iso_now(), other["turn_id"]),
                )
                _cancel_stale_public_outbox(
                    conn, turn_id=str(other["turn_id"]), reason="human_soft_group_hold"
                )
                set_case_communication(
                    conn,
                    str(other["case_id"]),
                    owner="human",
                    mode="silent",
                    reason=external_id,
                )
                _record_case_event(
                    conn,
                    case_id=str(other["case_id"]),
                    actor_id=actor_id,
                    event_type="communication_preempted",
                    detail={
                        "turn_id": other["turn_id"],
                        "signal": "soft",
                        "action": "claim",
                        "ambiguous_group_activity": True,
                        "investigation_continues": True,
                    },
                    idempotency_key=f"operator:{external_id}:turn:{other['turn_id']}",
                )
    return {
        "matched": True,
        "created": created,
        "signal": signal,
        "action": action,
        "turn_id": turn["turn_id"],
    }


def record_operator_reaction(
    conn: sqlite3.Connection,
    *,
    reaction_id: str,
    actor_id: str,
    source_message_id: str,
    emoji_type: str,
    occurred_at: str,
) -> dict[str, Any]:
    turn = conn.execute(
        """SELECT * FROM conversation_turns WHERE source_message_id=?
             AND state IN ('open','ai_scheduled','ai_sending','human_hold')
             ORDER BY created_at DESC LIMIT 1""",
        (source_message_id,),
    ).fetchone()
    if turn is None:
        return {"matched": False, "reason": "no_active_turn"}
    with transaction(conn):
        created = _apply_human_claim(
            conn,
            turn=turn,
            activity_type="reaction",
            signal="hard",
            action="claim",
            external_id=f"feishu-reaction:{reaction_id}",
            actor_id=actor_id,
            occurred_at=occurred_at,
            message_id=source_message_id,
            chat_id=turn["chat_id"],
            detail={"emoji_type": emoji_type},
        )
    return {
        "matched": True,
        "created": created,
        "signal": "hard",
        "action": "claim",
        "turn_id": turn["turn_id"],
    }


def control_communication(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    action: str,
    actor_id: str,
    external_id: str,
    turn_id: str | None = None,
    expected_case_version: int | None = None,
    expected_round: int | None = None,
    expected_fence: int | None = None,
) -> dict[str, Any]:
    from .delivery_attempts import in_flight_deliveries
    from .lifecycle import _atomic

    if action not in {"claim", "suggest_only", "delegate", "details"}:
        raise CoordinationError("unsupported communication action")
    case = conn.execute(
        "SELECT state FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None:
        raise CoordinationError("Case not found")
    if action == "delegate" and case["state"] in {"resolved", "takeover", "cancelled"}:
        raise CoordinationError("terminal Case cannot be delegated")
    turns = conn.execute(
        "SELECT * FROM conversation_turns WHERE case_id=? ORDER BY created_at DESC",
        (case_id,),
    ).fetchall()
    if not turns:
        raise CoordinationError("Case has no Feishu conversation turn")
    if action == "details":
        return {
            "command": "details",
            "case_id": case_id,
            "turns": [dict(row) for row in turns[:10]],
            "in_flight_deliveries": in_flight_deliveries(conn, case_id=case_id),
        }
    if turn_id is None:
        turn = turns[0]
    else:
        turn = next((row for row in turns if row["turn_id"] == turn_id), None)
        if turn is None:
            raise CoordinationError("Case conversation turn not found")
    with _atomic(conn):
        live_case = conn.execute(
            "SELECT version,lifecycle_round,state FROM cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        from .content_retirement import require_current_turn_after_retirement, ContentRetiredError
        try:
            require_current_turn_after_retirement(conn, case_id=case_id,
                lifecycle_round=live_case['lifecycle_round'], turn_id=turn['turn_id'])
        except ContentRetiredError as exc:
            raise CoordinationError(str(exc)) from exc
        if action == "delegate" and live_case["state"] in {
            "resolved",
            "takeover",
            "cancelled",
        }:
            raise CoordinationError("terminal Case cannot be delegated")
        live_turn = conn.execute(
            "SELECT fence FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()
        if (
            (
                expected_case_version is not None
                and live_case["version"] != expected_case_version
            )
            or (
                expected_round is not None
                and live_case["lifecycle_round"] != expected_round
            )
            or (expected_fence is not None and live_turn["fence"] != expected_fence)
        ):
            raise CoordinationError(
                "stale Case lifecycle control; refresh Case details"
            )
        existing = conn.execute(
            "SELECT 1 FROM operator_activities WHERE external_id=?", (external_id,)
        ).fetchone()
        if existing:
            current = conn.execute(
                "SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
            ).fetchone()
            return {
                "command": action,
                "case_id": case_id,
                "turn": dict(current),
                "replayed": True,
                "in_flight_deliveries": in_flight_deliveries(conn, case_id=case_id),
            }
        mode = (
            "suggest_only"
            if action == "suggest_only"
            else ("respond" if action == "delegate" else "silent")
        )
        owner = "ai" if action == "delegate" else "human"
        state = "open" if action == "delegate" else "human_hold"
        now = iso_now()
        conn.execute(
            """UPDATE conversation_turns SET communication_owner=?,communication_mode=?,state=?,
                   fence=fence+1,revision=revision+1,claimed_at=?,updated_at=? WHERE turn_id=?""",
            (
                owner,
                mode,
                state,
                now if owner == "human" else None,
                now,
                turn["turn_id"],
            ),
        )
        conn.execute(
            """INSERT INTO operator_activities(activity_id,external_id,activity_type,signal,action,
                   matched_turn_id,actor_id,occurred_at,detail_json,created_at)
               VALUES(?,?,'telegram_control','explicit',?,?,?,?, '{}',?)""",
            (new_id("opa"), external_id, action, turn["turn_id"], actor_id, now, now),
        )
        if action == "delegate":
            conn.execute("UPDATE cases SET owner='hermes' WHERE case_id=?", (case_id,))
            runtime = conn.execute(
                "SELECT outbound_fence FROM global_control_state WHERE scope='feishu_support'"
            ).fetchone()
            rebound = conn.execute(
                """UPDATE outbox SET turn_revision=?,communication_fence=?,
                          global_outbound_fence=?,updated_at=?
                     WHERE turn_id=? AND channel='feishu_im'
                       AND action_type IN ('reply','ack','clarify')
                       AND state IN ('pending','retry')""",
                (
                    int(turn["revision"]) + 1,
                    int(turn["fence"]) + 1,
                    int(runtime[0]) if runtime is not None else 1,
                    now,
                    turn["turn_id"],
                ),
            ).rowcount
            if rebound:
                conn.execute(
                    "UPDATE conversation_turns SET state='ai_scheduled',updated_at=? WHERE turn_id=?",
                    (now, turn["turn_id"]),
                )
        else:
            _cancel_stale_public_outbox(
                conn, turn_id=turn["turn_id"], reason=f"telegram_{action}"
            )
        current = conn.execute(
            "SELECT * FROM conversation_turns WHERE turn_id=?", (turn["turn_id"],)
        ).fetchone()
        _record_case_event(
            conn,
            case_id=case_id,
            actor_id=actor_id,
            event_type="communication_authority_changed",
            detail={
                "turn_id": turn["turn_id"],
                "action": action,
                "owner": owner,
                "mode": mode,
            },
            idempotency_key=f"operator:{external_id}",
        )
        set_case_communication(
            conn, case_id, owner=owner, mode=mode, reason=external_id
        )
    return {
        "command": action,
        "case_id": case_id,
        "turn": dict(current),
        "replayed": False,
        "in_flight_deliveries": in_flight_deliveries(conn, case_id=case_id),
    }


def validate_outbox_fence(
    conn: sqlite3.Connection, row: dict[str, Any]
) -> tuple[bool, str | None]:
    if row.get("channel") == "feishu_im" and row.get("action_type") in PUBLIC_ACTIONS:
        context = resolve_event_context(conn, row.get("source_event_pk"))
        source = conn.execute(
            "SELECT source FROM inbound_events WHERE event_pk=?",
            (row.get("source_event_pk"),),
        ).fetchone()
        actual_im = source is not None and source[0] in {
            "feishu_bot_im",
            "feishu_user_poll",
        }
        if actual_im and context is None:
            return False, "context_binding_missing"
        if context is not None or row.get("context_id"):
            if (
                context is None
                or context["context_id"] != row.get("context_id")
                or context["case_id"] != row.get("case_id")
            ):
                return False, "context_source_binding_mismatch"
            valid, reason = validate_context_binding(conn, row, require_ai=True)
            if not valid:
                return False, reason
    if row.get("channel") in {"telegram", "feishu_im"} and row.get("action_type") == "owner_decision":
        payload = json.loads(row.get("payload_json") or "{}")
        if payload.get('research_parent_job_id') is not None:
            from .retrieval import validate_retrieval_binding

            parent_id = payload['research_parent_job_id']
            parent = conn.execute('SELECT case_id FROM jobs WHERE job_id=?', (parent_id,)).fetchone()
            if parent is None or parent['case_id'] != row.get('case_id'):
                return False, 'research_handoff_parent_mismatch'
            valid, _ = validate_retrieval_binding(conn, parent_id, require_ai=True)
            if not valid:
                return False, 'research_handoff_superseded'
        if payload.get('context_recheck_event_id') is not None:
            from .context_recovery import validate_notice

            if not validate_notice(conn, row, payload):
                return False, 'context_recheck_notice_superseded'
        block_id = payload.get("delivery_block_outbox_id")
        if block_id is not None:
            block = conn.execute(
                """SELECT 1 FROM active_delivery_blocks
                   WHERE outbox_id=? AND notification_outbox_id=? AND case_id=?
                     AND lifecycle_round=? AND turn_id=? AND communication_fence=?""",
                (
                    block_id,
                    row.get("outbox_id"),
                    row.get("case_id"),
                    row.get("lifecycle_round"),
                    payload.get("control_turn_id"),
                    payload.get("control_fence"),
                ),
            ).fetchone()
            if block is None:
                return False, "delivery_block_notice_superseded"
    runtime = conn.execute(
        "SELECT outbound_fence FROM global_control_state WHERE scope='feishu_support'"
    ).fetchone()
    if (
        runtime is not None
        and row.get("channel") == "feishu_im"
        and row.get("action_type") in PUBLIC_ACTIONS
        and int(runtime[0]) != int(row.get("global_outbound_fence") or -1)
    ):
        return False, "global_outbound_fence_changed"
    turn_id = row.get("turn_id")
    if not turn_id:
        return True, None
    turn = conn.execute(
        "SELECT * FROM conversation_turns WHERE turn_id=?", (turn_id,)
    ).fetchone()
    if turn is None:
        return False, "turn_missing"
    if turn["communication_owner"] != "ai" or turn["communication_mode"] != "respond":
        return False, "communication_authority_revoked"
    if int(turn["revision"]) != int(row.get("turn_revision") or -1):
        return False, "turn_revision_changed"
    if int(turn["fence"]) != int(row.get("communication_fence") or -1):
        return False, "communication_fence_changed"
    if turn["state"] not in {"ai_scheduled", "ai_sending"}:
        return False, f"turn_state_{turn['state']}"
    return True, None


def suppress_outbox(conn: sqlite3.Connection, *, outbox_id: str, reason: str) -> None:
    conn.execute(
        """UPDATE outbox SET state='cancelled',suppression_reason=?,lease_owner=NULL,
               lease_expires_at=NULL,updated_at=? WHERE outbox_id=? AND state<>'delivered'""",
        (reason, iso_now(), outbox_id),
    )
