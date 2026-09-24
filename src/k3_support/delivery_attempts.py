"""Durable claim identity and append-only transport evidence.

These helpers run inside their caller's short transaction. They do not hold a
database lock during network activity and cannot undo a call already started.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from .ids import canonical_json, digest, new_id
from .timeutil import iso_now


def action_digest(row: dict[str, Any]) -> str:
    context_keys = ('context_id', 'context_revision', 'context_digest')
    # Preserve the digest of existing attempts with no context, including their
    # late receipts. Legacy send eligibility is checked separately, not upgraded.
    context = {key: row.get(key) for key in context_keys} if any(row.get(key) is not None for key in context_keys) else {}
    return digest(
        {
            **context,
            **{
            key: row.get(key)
            for key in (
                "outbox_id", "channel", "action_type", "destination", "payload_json",
                "idempotency_key", "case_id", "source_event_pk", "turn_id",
                "turn_revision", "communication_fence", "global_outbound_fence",
            )
            },
        }
    )


def owns_claim(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    token = row.get("claim_token")
    if not token or not row.get("lease_owner"):
        return False
    live = conn.execute(
        """SELECT o.*,a.action_digest AS claimed_digest FROM outbox o
           JOIN outbox_attempts a ON a.claim_token=o.claim_token
           WHERE o.outbox_id=? AND o.claim_token=? AND o.state='sending'
             AND o.lease_owner=? AND o.attempt_count=?""",
        (row["outbox_id"], token, row["lease_owner"], row["attempt_count"]),
    ).fetchone()
    if live is None:
        return False
    try:
        expires = datetime.fromisoformat(str(live["lease_expires_at"]))
        if expires.tzinfo is None or expires.astimezone(UTC) <= datetime.now(UTC):
            return False
    except (TypeError, ValueError):
        return False
    return live["claimed_digest"] == action_digest(dict(live)) == action_digest(row)


def start_attempt(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    if not owns_claim(conn, row):
        return False
    now = iso_now()
    changed = conn.execute(
        """UPDATE outbox_attempts SET dispatch_started_at=?
           WHERE claim_token=? AND dispatch_started_at IS NULL""",
        (now, row["claim_token"]),
    ).rowcount
    if not changed:
        return False
    conn.execute(
        "UPDATE outbox SET dispatch_started_at=?,updated_at=? WHERE outbox_id=? AND claim_token=?",
        (now, now, row["outbox_id"], row["claim_token"]),
    )
    return True


def record_outcome(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    event_type: str,
    detail: dict[str, Any],
    remote_id: str | None = None,
) -> None:
    if not row.get("claim_token"):
        return
    conn.execute(
        """INSERT OR IGNORE INTO outbox_attempt_events(
               event_id,claim_token,event_type,remote_message_id,detail_json,recorded_at)
           VALUES(?,?,?,?,?,?)""",
        (
            new_id("oae"), row["claim_token"], event_type, remote_id,
            canonical_json(detail), iso_now(),
        ),
    )


def in_flight_deliveries(conn: sqlite3.Connection, *, case_id: str) -> list[dict[str, Any]]:
    """Operator-visible uncertainty, not permission to retry or retract a send."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT a.outbox_id,a.claim_token,a.dispatch_started_at,
                      CASE WHEN EXISTS(SELECT 1 FROM outbox_attempt_events e
                        WHERE e.claim_token=a.claim_token AND e.event_type IN ('uncertain','lease_expired'))
                        THEN 'uncertain' ELSE 'in_flight' END AS state
               FROM outbox_attempts a JOIN outbox o ON o.outbox_id=a.outbox_id
               WHERE o.case_id=? AND o.channel='feishu_im'
                 AND o.action_type IN ('reply','ack','clarify')
                 AND a.dispatch_started_at IS NOT NULL
                 AND NOT EXISTS(SELECT 1 FROM outbox_attempt_events e
                   WHERE e.claim_token=a.claim_token AND e.event_type IN ('delivered','failed'))
               ORDER BY a.dispatch_started_at,a.claim_token""",
            (case_id,),
        )
    ]
