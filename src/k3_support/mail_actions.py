"""Local operator dispositions; never mutate mailbox flags or digest snapshots.

Callers must authenticate the operator before invoking apply().
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .db import transaction
from .ids import canonical_json
from .mail_catalog import CATEGORIES


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def page(conn, *, after_id="", limit=30, category="all", state="all", now=None):
    """Live keyset listing; explicitly not an immutable historical digest."""
    if not isinstance(after_id, str) or len(after_id) > 1024:
        raise ValueError("invalid cursor")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid limit")
    if category not in CATEGORIES | {"all", "unclassified"}:
        raise ValueError("invalid category")
    if state not in {"all", "todo", "snoozed", "done"}:
        raise ValueError("invalid state")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    rows = conn.execute(
        "WITH entries AS (SELECT m.message_id,coalesce(c.category,'unclassified') AS category,"
        "CASE WHEN a.state='snoozed' AND julianday(a.snooze_until)<=julianday(?) THEN 'todo' "
        "ELSE coalesce(a.state,'todo') END AS effective_state "
        "FROM (SELECT message_id FROM mail_catalog_items UNION SELECT message_id FROM mail_items) m "
        "LEFT JOIN mail_catalog_items c ON c.message_id=m.message_id "
        "LEFT JOIN mail_action_state a ON a.message_id=m.message_id) "
        "SELECT message_id FROM entries WHERE message_id>? "
        "AND (?='all' OR category=?) AND (?='all' OR effective_state=?) "
        "ORDER BY message_id LIMIT ?",
        (now.isoformat(), after_id, category, category, state, state, limit + 1),
    ).fetchall()
    return {
        "items": [view(conn, row[0], now=now) for row in rows[:limit]],
        "next_cursor": rows[limit - 1][0] if len(rows) > limit else None,
        "live": True,
        "filters": {"category": category, "state": state},
    }


def view(conn, message_id, *, now=None):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    mail = conn.execute(
        "SELECT message_id,thread_id,sender_name,sender_address,subject,internal_date,category,attention "
        "FROM mail_catalog_items WHERE message_id=?",
        (message_id,),
    ).fetchone()
    if mail is None:
        mail = conn.execute(
            "SELECT message_id,thread_id,sender_name,sender_address,subject,internal_date,"
            "'unclassified' AS category,NULL AS attention "
            "FROM mail_items WHERE message_id=?",
            (message_id,),
        ).fetchone()
    if mail is None:
        raise ValueError("mail not found")
    header = dict(mail)
    row = conn.execute(
        "SELECT * FROM mail_action_state WHERE message_id=?", (message_id,)
    ).fetchone()
    state = (
        dict(row)
        if row
        else {
            "message_id": message_id,
            "revision": 0,
            "state": "todo",
            "snooze_until": None,
            "linked_case_id": None,
            "updated_by": None,
            "updated_at": None,
        }
    )
    return {
        "header": header,
        "content_digest": _digest(header),
        **state,
        "effective_state": "todo"
        if state["state"] == "snoozed"
        and datetime.fromisoformat(state["snooze_until"]) <= now
        else state["state"],
        "mailbox_changed": False,
    }


def apply(
    conn,
    *,
    message_id,
    action,
    expected_revision,
    content_digest,
    request_id,
    actor_id,
    minutes=None,
    case_id=None,
    now=None,
):
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("operator required")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("canonical request UUID required")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("invalid revision")
    if action not in {"done", "reopen", "snooze", "link_case", "unlink_case"}:
        raise ValueError("invalid action")
    if action == "snooze":
        if type(minutes) is not int or not 1 <= minutes <= 43200:
            raise ValueError("snooze requires 1..43200 minutes")
    elif minutes is not None:
        raise ValueError("unexpected minutes")
    if (action == "link_case" and not isinstance(case_id, str)) or (
        action != "link_case" and case_id is not None
    ):
        raise ValueError("invalid case argument")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    now = now.astimezone(UTC)
    fingerprint = _digest(
        {
            "message_id": message_id,
            "action": action,
            "revision": expected_revision,
            "content_digest": content_digest,
            "actor": actor_id,
            "minutes": minutes,
            "case_id": case_id,
        }
    )
    with transaction(conn):
        old = conn.execute(
            "SELECT * FROM mail_action_history WHERE request_id=?", (request_id,)
        ).fetchone()
        if old:
            if old["request_digest"] != fingerprint:
                raise ValueError("request ID reused with different action")
            return {**json.loads(old["result_json"]), "replayed": True}
        current = view(conn, message_id, now=now)
        if (
            current["revision"] != expected_revision
            or current["content_digest"] != content_digest
        ):
            raise ValueError("mail changed; refresh before acting")
        state, until, linked = (
            current["state"],
            current["snooze_until"],
            current["linked_case_id"],
        )
        if action in {"done", "reopen"}:
            state, until = ("done" if action == "done" else "todo"), None
        elif action == "snooze":
            state, until = "snoozed", (now + timedelta(minutes=minutes)).isoformat()
        elif action == "unlink_case":
            linked = None
        else:
            if not conn.execute(
                "SELECT 1 FROM cases WHERE case_id=?", (case_id,)
            ).fetchone():
                raise ValueError("case not found")
            linked = case_id
        conn.execute(
            "INSERT INTO mail_action_state VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(message_id) DO UPDATE SET revision=excluded.revision,"
            "state=excluded.state,snooze_until=excluded.snooze_until,"
            "linked_case_id=excluded.linked_case_id,updated_by=excluded.updated_by,"
            "updated_at=excluded.updated_at",
            (
                message_id,
                expected_revision + 1,
                state,
                until,
                linked,
                actor_id,
                now.isoformat(),
            ),
        )
        result = view(conn, message_id, now=now)
        conn.execute(
            "INSERT INTO mail_action_history VALUES(?,?,?,?,?)",
            (
                request_id,
                fingerprint,
                message_id,
                expected_revision + 1,
                canonical_json(result),
            ),
        )
        return {**result, "replayed": False}
