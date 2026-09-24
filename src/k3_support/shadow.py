from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import transaction
from .ids import canonical_json, new_id
from .timeutil import epoch_now, iso_now


class ShadowError(ValueError):
    pass


def _counts(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    where: str,
    args: tuple[Any, ...],
) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT {column},count(*) FROM {table} WHERE {where} GROUP BY {column}",
            args,
        )
    }


def report(conn: sqlite3.Connection, *, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    if days < 1 or days > 90:
        raise ShadowError("days must be between 1 and 90")
    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    start_dt = now_dt - timedelta(days=days)
    start_epoch = int(start_dt.timestamp())
    start_iso = start_dt.isoformat(timespec="milliseconds")

    first_epoch = conn.execute(
        "SELECT min(received_epoch) FROM inbound_events"
    ).fetchone()[0]
    observed_seconds = max(0, int(now_dt.timestamp()) - int(first_epoch or int(now_dt.timestamp())))
    inbound_total = int(
        conn.execute(
            "SELECT count(*) FROM inbound_events WHERE received_epoch>=?", (start_epoch,)
        ).fetchone()[0]
    )
    case_total = int(
        conn.execute("SELECT count(*) FROM cases WHERE created_epoch>=?", (start_epoch,)).fetchone()[0]
    )
    suggestion_rows = conn.execute(
        """SELECT status,count(*) FROM case_suggestions
             WHERE created_at>=? GROUP BY status""",
        (start_iso,),
    ).fetchall()
    suggestion_status = {str(row[0]): int(row[1]) for row in suggestion_rows}
    accepted = suggestion_status.get("accepted", 0)
    rejected = suggestion_status.get("rejected", 0)
    reviewed = accepted + rejected
    precision = accepted / reviewed if reviewed else None
    dead_letters = int(
        conn.execute(
            "SELECT count(*) FROM inbound_events WHERE received_epoch>=? AND status='dead_letter'",
            (start_epoch,),
        ).fetchone()[0]
    )
    pending_outbox = int(
        conn.execute(
            """SELECT count(*) FROM outbox
                 WHERE created_at>=? AND state IN ('pending','sending','retry')""",
            (start_iso,),
        ).fetchone()[0]
    )
    active_canaries = int(
        conn.execute(
            """SELECT count(*) FROM cases
                 WHERE title LIKE '%[CANARY]%' AND state NOT IN ('resolved','cancelled','takeover')"""
        ).fetchone()[0]
    )
    high_confidence_unreviewed = int(
        conn.execute(
            """SELECT count(*) FROM case_suggestions
                 WHERE created_at>=? AND status='shadow' AND confidence>=0.85
                   AND kind IN ('knowledge_answer','reply_draft')""",
            (start_iso,),
        ).fetchone()[0]
    )
    observed_days = observed_seconds / 86400
    precision_gate = precision is not None and precision >= 0.95
    route_status = _counts(
        conn,
        table="route_decisions",
        column="review_status",
        where="created_at>=?",
        args=(start_iso,),
    )
    route_accepted = route_status.get("accepted", 0)
    route_rejected = route_status.get("rejected", 0)
    route_reviewed = route_accepted + route_rejected
    route_precision = route_accepted / route_reviewed if route_reviewed else None
    route_precision_gate = route_precision is not None and route_precision >= 0.95
    observation_gate = observed_days >= 7
    return {
        "window": {
            "days": days,
            "start": start_dt.isoformat(),
            "end": now_dt.isoformat(),
            "first_event_epoch": first_epoch,
            "observed_seconds": observed_seconds,
            "observed_days": round(observed_days, 3),
        },
        "inbound": {
            "total": inbound_total,
            "by_source": _counts(
                conn,
                table="inbound_events",
                column="source",
                where="received_epoch>=?",
                args=(start_epoch,),
            ),
            "by_status": _counts(
                conn,
                table="inbound_events",
                column="status",
                where="received_epoch>=?",
                args=(start_epoch,),
            ),
            "dead_letters": dead_letters,
        },
        "cases": {
            "total": case_total,
            "by_state": _counts(
                conn,
                table="cases",
                column="state",
                where="created_epoch>=?",
                args=(start_epoch,),
            ),
            "by_type": _counts(
                conn,
                table="cases",
                column="type",
                where="created_epoch>=?",
                args=(start_epoch,),
            ),
            "active_canaries": active_canaries,
        },
        "suggestions": {
            "by_kind": _counts(
                conn,
                table="case_suggestions",
                column="kind",
                where="created_at>=?",
                args=(start_iso,),
            ),
            "by_status": suggestion_status,
            "reviewed": reviewed,
            "accepted": accepted,
            "rejected": rejected,
            "precision": precision,
            "high_confidence_unreviewed": high_confidence_unreviewed,
        },
        "routing": {
            "by_route": _counts(
                conn,
                table="route_decisions",
                column="route",
                where="created_at>=?",
                args=(start_iso,),
            ),
            "by_proposed_route": _counts(
                conn,
                table="route_decisions",
                column="proposed_route",
                where="created_at>=?",
                args=(start_iso,),
            ),
            "by_review_status": route_status,
            "reviewed": route_reviewed,
            "accepted": route_accepted,
            "rejected": route_rejected,
            "precision": route_precision,
        },
        "delivery": {
            "by_state": _counts(
                conn,
                table="outbox",
                column="state",
                where="created_at>=?",
                args=(start_iso,),
            ),
            "pending": pending_outbox,
        },
        "gates": {
            "observation_7d": observation_gate,
            "reviewed_sample_present": reviewed > 0,
            "precision_at_least_95pct": precision_gate,
            "routing_reviewed_sample_present": route_reviewed > 0,
            "routing_precision_at_least_95pct": route_precision_gate,
            "no_dead_letters": dead_letters == 0,
            "no_pending_outbox": pending_outbox == 0,
            "no_active_canaries": active_canaries == 0,
            "ready_for_faq": all(
                (
                    observation_gate,
                    reviewed > 0,
                    precision_gate,
                    route_reviewed > 0,
                    route_precision_gate,
                    dead_letters == 0,
                    pending_outbox == 0,
                    active_canaries == 0,
                )
            ),
        },
    }


def review_suggestion(
    conn: sqlite3.Connection,
    *,
    suggestion_id: str,
    decision: str,
    reviewer_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    if decision not in {"accepted", "rejected"}:
        raise ShadowError("decision must be accepted or rejected")
    now = iso_now()
    idempotency_key = f"shadow-review:{suggestion_id}:{decision}"
    with transaction(conn):
        suggestion = conn.execute(
            "SELECT case_id,status FROM case_suggestions WHERE suggestion_id=?",
            (suggestion_id,),
        ).fetchone()
        if suggestion is None:
            raise ShadowError("suggestion not found")
        previous = conn.execute(
            "SELECT event_id FROM case_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if previous is not None:
            return {
                "suggestion_id": suggestion_id,
                "status": decision,
                "changed": False,
                "event_id": str(previous["event_id"]),
            }
        if suggestion["status"] == "expired":
            raise ShadowError("expired suggestion cannot be reviewed")
        conn.execute(
            "UPDATE case_suggestions SET status=? WHERE suggestion_id=?",
            (decision, suggestion_id),
        )
        sequence = int(
            conn.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
                (suggestion["case_id"],),
            ).fetchone()[0]
        )
        event_id = new_id("cev")
        conn.execute(
            """INSERT INTO case_events(event_id,case_id,sequence,event_type,actor_type,actor_id,
                   detail_json,idempotency_key,created_at,created_epoch)
               VALUES(?,?,?,'shadow_suggestion_reviewed','operator',?,?,?,?,?)""",
            (
                event_id,
                suggestion["case_id"],
                sequence,
                reviewer_id,
                canonical_json(
                    {
                        "suggestion_id": suggestion_id,
                        "before": suggestion["status"],
                        "decision": decision,
                        "note": note,
                    }
                ),
                idempotency_key,
                now,
                epoch_now(),
            ),
        )
    return {
        "suggestion_id": suggestion_id,
        "status": decision,
        "changed": True,
        "event_id": event_id,
    }
