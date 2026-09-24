"""Operator-owned local mail-category subscriptions; no transport or mail reads."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .db import transaction
from .ids import canonical_json, digest
from .mail_catalog import CATEGORIES


def configure(
    conn,
    *,
    owner_id,
    category,
    enabled,
    expected_revision,
    request_id,
    snooze_minutes=None,
    now=None,
):
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 512:
        raise ValueError("operator required")
    if (
        not isinstance(category, str)
        or category not in CATEGORIES
        or type(enabled) is not bool
    ):
        raise ValueError("invalid subscription")
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or not isinstance(request_id, str)
        or str(UUID(request_id)) != request_id
    ):
        raise ValueError("invalid revision or request")
    if snooze_minutes is not None and (
        type(snooze_minutes) is not int or not 1 <= snooze_minutes <= 10080
    ):
        raise ValueError("invalid snooze")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    binding = digest([owner_id, category, enabled, expected_revision, snooze_minutes])
    sid = "sub_" + digest([owner_id, category])[:32]
    with transaction(conn):
        old = conn.execute(
            "SELECT * FROM attention_subscription_history WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if old:
            if old["request_digest"] != binding:
                raise ValueError("request binding changed")
            return json.loads(old["result_json"])
        row = conn.execute(
            "SELECT * FROM attention_subscriptions WHERE subscription_id=?", (sid,)
        ).fetchone()
        if (row["revision"] if row else 0) != expected_revision:
            raise ValueError("subscription changed; reread")
        until = (
            (now + timedelta(minutes=snooze_minutes)).isoformat()
            if snooze_minutes
            else None
        )
        conn.execute(
            "INSERT INTO attention_subscriptions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled,snooze_until=excluded.snooze_until,updated_at=excluded.updated_at",
            (
                sid,
                owner_id,
                category,
                expected_revision + 1,
                int(enabled),
                until,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        result = dict(
            conn.execute(
                "SELECT * FROM attention_subscriptions WHERE subscription_id=?", (sid,)
            ).fetchone()
        )
        conn.execute(
            "INSERT INTO attention_subscription_history VALUES(?,?,?)",
            (request_id, binding, canonical_json(result)),
        )
        return result


def collect(conn, *, now=None, limit=100, owner_id=None):
    """Materialize unseen matches from the already-collected catalog, bounded per call."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 500
        or (
            owner_id is not None
            and (not isinstance(owner_id, str) or not owner_id.strip())
        )
    ):
        raise ValueError("invalid limit")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    with transaction(conn):
        rows = conn.execute(
            "SELECT s.subscription_id,m.message_id FROM attention_subscriptions s JOIN mail_catalog_items m ON m.category=s.category "
            "WHERE s.enabled=1 AND (? IS NULL OR s.owner_id=?) AND (s.snooze_until IS NULL OR julianday(s.snooze_until)<=julianday(?)) "
            "AND julianday(m.first_seen_at)>julianday(s.created_at) "
            "AND NOT EXISTS(SELECT 1 FROM attention_actions a WHERE a.subscription_id=s.subscription_id AND a.message_id=m.message_id) "
            "ORDER BY m.first_seen_at,s.subscription_id,m.message_id LIMIT ?",
            (owner_id, owner_id, now.isoformat(), limit),
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO attention_actions VALUES(?,?,?,?)",
                ("att_" + digest(list(row))[:32], *row, now.isoformat()),
            )
    return {"created": len(rows), "external_messages_sent": 0}


def detail(conn, *, owner_id, action_id, now=None):
    from .mail_actions import view

    if (
        not isinstance(owner_id, str)
        or not owner_id.strip()
        or not isinstance(action_id, str)
        or len(action_id) > 128
    ):
        raise ValueError("invalid attention target")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    row = conn.execute(
        "SELECT a.message_id FROM attention_actions a JOIN attention_subscriptions s ON s.subscription_id=a.subscription_id "
        "WHERE a.action_id=? AND s.owner_id=? AND s.enabled=1 "
        "AND (s.snooze_until IS NULL OR julianday(s.snooze_until)<=julianday(?))",
        (action_id, owner_id, now.isoformat()),
    ).fetchone()
    if row is None:
        raise ValueError("关注事项不可用，请重新读取")
    return view(conn, row[0], now=now)


def page(conn, *, owner_id, after_id="", limit=30, now=None):
    if (
        not isinstance(owner_id, str)
        or not owner_id.strip()
        or not isinstance(after_id, str)
        or len(after_id) > 128
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("invalid action filter")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    rows = conn.execute(
        "SELECT a.action_id,a.message_id,s.category,m.subject,m.sender_name FROM attention_actions a "
        "JOIN attention_subscriptions s ON s.subscription_id=a.subscription_id JOIN mail_catalog_items m ON m.message_id=a.message_id "
        "LEFT JOIN mail_action_state d ON d.message_id=m.message_id "
        "WHERE s.owner_id=? AND s.enabled=1 AND a.action_id>? "
        "AND (s.snooze_until IS NULL OR julianday(s.snooze_until)<=julianday(?)) "
        "AND (d.state IS NULL OR d.state='todo' OR (d.state='snoozed' AND julianday(d.snooze_until)<=julianday(?))) "
        "ORDER BY a.action_id LIMIT ?",
        (owner_id, after_id, now.isoformat(), now.isoformat(), limit + 1),
    ).fetchall()
    return {
        "items": [dict(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1]["action_id"] if len(rows) > limit else None,
        "read_only": True,
    }
