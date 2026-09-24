"""Explicit operator outcome and authority-round changes, never AI permission."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

from .delivery_attempts import in_flight_deliveries
from .ids import canonical_json, digest, new_id
from .timeutil import epoch_now, iso_now


class LifecycleError(ValueError):
    pass


@contextmanager
def _atomic(conn):
    outer = conn.in_transaction
    conn.execute("SAVEPOINT case_lifecycle" if outer else "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO case_lifecycle" if outer else "ROLLBACK")
        if outer:
            conn.execute("RELEASE case_lifecycle")
        raise
    else:
        conn.execute("RELEASE case_lifecycle" if outer else "COMMIT")


def _event(conn, *, case, event_type, actor_id, after, detail, key, now):
    sequence = conn.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
        (case["case_id"],),
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,
               actor_id,before_state,after_state,detail_json,idempotency_key,created_at,created_epoch)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("cev"),
            case["case_id"],
            sequence,
            event_type,
            "operator" if actor_id else "system",
            actor_id,
            case["state"],
            after,
            canonical_json(detail),
            key,
            now,
            epoch_now(),
        ),
    )


def operator_transition(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    action: str,
    expected_version: int,
    actor_id: str,
    idempotency_key: str,
    reason: str = "",
) -> dict[str, Any]:
    """Caller must authenticate the control identity. Never delegate implicitly."""
    if action not in {"resolve", "reopen"} or not actor_id or not idempotency_key:
        raise LifecycleError("invalid Case lifecycle action")
    request = digest(
        {
            "case_id": case_id,
            "action": action,
            "expected_version": expected_version,
            "actor_id": actor_id,
            "reason": reason,
        }
    )
    with _atomic(conn):
        prior = conn.execute(
            "SELECT * FROM case_lifecycle_actions WHERE external_id=?",
            (idempotency_key,),
        ).fetchone()
        if prior:
            if prior["request_digest"] != request:
                raise LifecycleError(
                    "lifecycle action ID was reused with different content"
                )
            return {
                **json.loads(prior["result_json"]),
                "replayed": True,
                "in_flight_deliveries": in_flight_deliveries(conn, case_id=case_id),
            }
        found = conn.execute(
            "SELECT * FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if found is None:
            raise LifecycleError("Case not found")
        case = dict(found)
        if isinstance(expected_version, bool) or case["version"] != expected_version:
            raise LifecycleError("stale Case lifecycle version; refresh Case details")
        if case["canonical_case_id"]:
            raise LifecycleError(
                "merged Case cannot be independently resolved or reopened"
            )
        if action == "reopen" and case["state"] not in {
            "resolved",
            "cancelled",
            "takeover",
        }:
            raise LifecycleError("reopen requires a closed or fully taken-over Case")
        if action == "resolve" and case["state"] in {"resolved", "cancelled"}:
            raise LifecycleError("Case is already closed")
        now = iso_now()
        after = "triage" if action == "reopen" else "resolved"
        round_number = case["lifecycle_round"] + int(action == "reopen")
        # The legacy schema calls its human ownership enum "operator". Actual
        # operator identity is always the separately authenticated actor_id.
        conn.execute(
            """UPDATE conversation_turns SET communication_owner='human',
                        communication_mode='silent',state='closed',revision=revision+1,
                        fence=fence+1,claimed_at=?,updated_at=? WHERE case_id=?""",
            (now, now, case_id),
        )
        turn = conn.execute(
            """SELECT * FROM conversation_turns WHERE case_id=?
                               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (case_id,),
        ).fetchone()
        if action == "reopen" and turn:
            conn.execute(
                "UPDATE conversation_turns SET state='human_hold' WHERE turn_id=?",
                (turn["turn_id"],),
            )
        conn.execute(
            """UPDATE jobs SET state='cancelled',error_class=?,lease_owner=NULL,
                        lease_expires_at=NULL,updated_at=? WHERE case_id=?
                        AND state IN ('queued','running','waiting')""",
            (f"case_{action}", now, case_id),
        )
        conn.execute(
            """UPDATE approvals SET status='revoked',updated_at=? WHERE case_id=?
                        AND status IN ('requested','approved')""",
            (now, case_id),
        )
        conn.execute(
            """UPDATE outbox SET state='cancelled',suppression_reason=?,lease_owner=NULL,
                        lease_expires_at=NULL,updated_at=? WHERE case_id=?
                        AND state IN ('pending','retry','sending')""",
            (f"case_{action}", now, case_id),
        )
        # Revoking Case authority does not establish physical board cleanup.
        # Preserve every device lock until the existing lease/cleanup mechanism
        # can account for any operation already in flight.
        conn.execute(
            """UPDATE cases SET state=?,owner='operator',version=version+1,lifecycle_round=?,
                        outcome=?,outcome_provenance=?,resolved_at=?,active_job_id=NULL,
                        active_session_id=NULL,next_action=?,last_material_progress_at=?,
                        updated_at=?,updated_epoch=? WHERE case_id=? AND version=?""",
            (
                after,
                round_number,
                "unknown" if action == "reopen" else "operator_resolved",
                "operator_reopened" if action == "reopen" else "operator_confirmed",
                None if action == "reopen" else now,
                "人工负责；如需 AI 继续，请另行交给 AI。"
                if action == "reopen"
                else None,
                now,
                now,
                epoch_now(),
                case_id,
                expected_version,
            ),
        )
        if action == "reopen":
            conn.execute(
                """INSERT INTO case_rounds(case_id,round_number,started_at,actor_id,
                            reason,initial_case_version,control_fence) VALUES(?,?,?,?,?,?,?)""",
                (
                    case_id,
                    round_number,
                    now,
                    actor_id,
                    reason,
                    expected_version + 1,
                    turn["fence"] if turn else None,
                ),
            )
        _event(
            conn,
            case=case,
            event_type=f"operator_{action}",
            actor_id=actor_id,
            after=after,
            detail={
                "reason": reason,
                "round": round_number,
                "previous_outcome": case["outcome"],
                "authority": "human; no previous work restored",
            },
            key=idempotency_key,
            now=now,
        )
        result = {
            "command": action,
            "case_id": case_id,
            "state": after,
            "version": expected_version + 1,
            "lifecycle_round": round_number,
            "outcome": "unknown" if action == "reopen" else "operator_resolved",
            "replayed": False,
        }
        conn.execute(
            """INSERT INTO case_lifecycle_actions(external_id,case_id,action,request_digest,
                        result_json,actor_id,created_at) VALUES(?,?,?,?,?,?,?)""",
            (
                idempotency_key,
                case_id,
                action,
                request,
                canonical_json(result),
                actor_id,
                now,
            ),
        )
        return {
            **result,
            "in_flight_deliveries": in_flight_deliveries(conn, case_id=case_id),
        }


def record_reply_delivered(
    conn: sqlite3.Connection,
    *,
    row: dict[str, Any],
    remote_message_id: str,
    delivered_at: str,
) -> bool:
    """Called inside the receipt transaction, after transport success is proven."""
    if (
        row.get("channel") != "feishu_im"
        or row.get("action_type") != "reply"
        or not row.get("case_id")
    ):
        return False
    found = conn.execute(
        "SELECT * FROM cases WHERE case_id=?", (row["case_id"],)
    ).fetchone()
    if found is None:
        return False
    case = dict(found)
    if case["state"] != "answering" or case["lifecycle_round"] != row.get(
        "lifecycle_round", 1
    ):
        return False
    turn = conn.execute(
        "SELECT * FROM conversation_turns WHERE turn_id=?", (row.get("turn_id"),)
    ).fetchone()
    if (
        not turn
        or turn["communication_owner"] != "ai"
        or turn["communication_mode"] != "respond"
        or (turn["revision"], turn["fence"])
        != (row.get("turn_revision"), row.get("communication_fence"))
    ):
        return False
    key = f"outbox:{row['outbox_id']}:answered"
    if conn.execute(
        "SELECT 1 FROM case_events WHERE idempotency_key=?", (key,)
    ).fetchone():
        return False
    board = conn.execute(
        "SELECT 1 FROM evidence WHERE case_id=? AND evidence_layer IN ('ram_boot','persistent_flash','device_function','stability') AND julianday(created_at)>=(SELECT julianday(started_at) FROM case_rounds WHERE case_id=evidence.case_id ORDER BY round_number DESC LIMIT 1) LIMIT 1",
        (case["case_id"],),
    ).fetchone()
    outcome = (
        "answered"
        if case["type"] == "faq"
        else ("awaiting_environment_comparison" if board else "awaiting_validation")
    )
    next_action = (
        "答复已送达；不自动追问或要求确认。"
        if outcome == "answered"
        else (
            "本板验证不代表对方现场解决；请核对双方板型、版本、启动介质和故障日志。"
            if board
            else "答复已送达；现场结果尚未确认，不自动追问。"
        )
    )
    conn.execute(
        """UPDATE cases SET state='monitoring',outcome=?,outcome_provenance='delivery_receipt',
                    version=version+1,last_public_update_at=?,last_material_progress_at=?,
                    next_action=?,updated_at=?,updated_epoch=? WHERE case_id=? AND version=?""",
        (
            outcome,
            delivered_at,
            delivered_at,
            next_action,
            delivered_at,
            epoch_now(),
            case["case_id"],
            case["version"],
        ),
    )
    _event(
        conn,
        case=case,
        event_type="reply_delivered",
        actor_id=None,
        after="monitoring",
        detail={
            "outbox_id": row["outbox_id"],
            "remote_message_id": remote_message_id,
            "outcome": outcome,
            "field_resolution_confirmed": False,
        },
        key=key,
        now=delivered_at,
    )
    return True


def record_investigation_handoff(conn, *, bundle, decision):
    """Store complete review data and explicit limits, without granting rights."""
    job = conn.execute(
        """SELECT j.lifecycle_round,c.lifecycle_round AS current_round,c.state
                          FROM jobs j JOIN cases c USING(case_id) WHERE j.job_id=?""",
        (bundle["job_id"],),
    ).fetchone()
    if (
        not job
        or job["lifecycle_round"] != job["current_round"]
        or job["state"] in {"resolved", "cancelled", "takeover", "paused"}
    ):
        raise LifecycleError("stale or paused investigation handoff")
    sections = bundle["sections"]
    checks = bundle.get("checks") or []
    content = {
        "facts": decision.get("facts") or [],
        "hypotheses": decision.get("inferences") or [],
        "unconfirmed_differences": decision.get("unknowns") or [],
        "environment_comparison": "对方板型、硬件版本、固件版本、启动介质和故障日志是否与本次验证一致：尚未由现场确认。",
        "evidence_boundary": "记录的静态/构建/本板结果仅覆盖对应环境；本板正常或未复现不等于对方现场已解决。",
        "independent_checks": checks,
        "evidence_ids": bundle.get("evidence_ids") or [],
        "codex_report_untrusted": {
            key: sections.get(key)
            for key in (
                "status",
                "root_cause",
                "changes",
                "verification",
                "board_state",
                "risks",
            )
        },
        "suggested_next_action": sections.get("next_action")
        or "需要人工判断下一步；不要自动重复索要信息。",
        "reply_status": "已进入发送流程，尚未取得送达回执"
        if decision["intent"] == "reply"
        else "没有代表你追问或回复",
    }
    conn.execute(
        """INSERT OR IGNORE INTO case_handoffs(review_id,case_id,lifecycle_round,content_json,created_at)
                    VALUES(?,?,?,?,?)""",
        (
            bundle["review_id"],
            bundle["case_id"],
            job["current_round"],
            canonical_json(content),
            iso_now(),
        ),
    )
    if decision["intent"] != "reply":
        if decision["intent"] == "wait" and job["state"] != "monitoring":
            before = dict(
                conn.execute(
                    "SELECT * FROM cases WHERE case_id=?", (bundle["case_id"],)
                ).fetchone()
            )
            conn.execute(
                "UPDATE cases SET state='monitoring',version=version+1,updated_at=? WHERE case_id=?",
                (iso_now(), bundle["case_id"]),
            )
            _event(
                conn,
                case=before,
                event_type="investigation_waiting",
                actor_id=None,
                after="monitoring",
                detail={
                    "review_id": bundle["review_id"],
                    "field_resolution_confirmed": False,
                },
                key=f"review:{bundle['review_id']}:waiting",
                now=iso_now(),
            )
        has_board = conn.execute(
            "SELECT 1 FROM evidence WHERE case_id=? AND evidence_layer IN ('ram_boot','persistent_flash','device_function','stability') AND evidence_id IN (SELECT value FROM json_each(?)) LIMIT 1",
            (bundle["case_id"], canonical_json(bundle.get("evidence_ids") or [])),
        ).fetchone()
        conn.execute(
            """UPDATE cases SET outcome=?,outcome_provenance='investigation_review',
                        next_action=?,last_material_progress_at=? WHERE case_id=? AND lifecycle_round=?
                        AND state NOT IN ('resolved','cancelled','takeover','paused')""",
            (
                "awaiting_environment_comparison"
                if has_board
                else "awaiting_validation",
                content["suggested_next_action"],
                iso_now(),
                bundle["case_id"],
                job["current_round"],
            ),
        )
    return content
