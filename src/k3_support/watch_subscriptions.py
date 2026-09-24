"""Versioned subscriptions to local release/Case sources; no external transport."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID

from .db import transaction
from .ids import canonical_json, digest


def mark_seen(conn, *, owner_id, action_id):
    """Idempotent acknowledgement of one immutable local action, not remote read state."""
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("operator required")
    if not isinstance(action_id, str) or not action_id or len(action_id) > 500:
        raise ValueError("invalid action")
    with transaction(conn):
        row = conn.execute("""SELECT a.action_id FROM watch_actions a
                              JOIN watch_subscriptions s USING(subscription_id)
                              WHERE a.action_id=? AND s.owner_id=?""", (action_id, owner_id)).fetchone()
        if not row:
            raise ValueError("action not available")
        conn.execute("INSERT OR IGNORE INTO watch_seen VALUES(?,?,?)", (action_id, owner_id, datetime.now(UTC).isoformat()))
        return dict(conn.execute("SELECT * FROM watch_seen WHERE action_id=? AND owner_id=?", (action_id, owner_id)).fetchone())


def settings(conn, *, owner_id, source_kind, source_key):
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 512:
        raise ValueError("operator required")
    if source_kind not in ("release", "case") or not isinstance(source_key, str) or not source_key or len(source_key) > 500:
        raise ValueError("invalid source")
    row = conn.execute("SELECT * FROM watch_subscriptions WHERE owner_id=? AND source_kind=? AND source_key=?",
                       (owner_id, source_kind, source_key)).fetchone()
    return dict(row) if row else {"source_kind": source_kind, "source_key": source_key,
                                 "revision": 0, "enabled": 0}


def page(conn, *, owner_id, after_id="", limit=30):
    """Read current enabled watches; omit deleted sources and all private event details."""
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("operator required")
    if not isinstance(after_id, str) or len(after_id) > 500 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid pagination")
    rows = conn.execute(
        """SELECT a.action_id,a.source_id,s.source_kind,s.source_key,
                  CASE WHEN s.source_kind='release' THEN r.subject ELSE e.event_type END AS title,
                  CASE WHEN s.source_kind='release' THEN r.created_at ELSE e.created_at END AS occurred_at,
                  r.revision,e.before_state,e.after_state
           FROM watch_actions a JOIN watch_subscriptions s USING(subscription_id)
           LEFT JOIN release_impacts r ON s.source_kind='release' AND r.impact_id=a.source_id
                AND r.repository=s.source_key
           LEFT JOIN case_events e ON s.source_kind='case' AND e.event_id=a.source_id
                AND e.case_id=s.source_key
           WHERE s.owner_id=? AND s.enabled=1 AND a.action_id>?
             AND NOT EXISTS(SELECT 1 FROM watch_seen seen WHERE seen.action_id=a.action_id AND seen.owner_id=s.owner_id)
             AND (r.impact_id IS NOT NULL OR e.event_id IS NOT NULL)
           ORDER BY a.action_id LIMIT ?""",
        (owner_id, after_id, limit + 1),
    ).fetchall()
    items = [dict(row) for row in rows[:limit]]
    return {"items": items, "next_after_id": items[-1]["action_id"] if len(rows) > limit else None,
            "note": "本地关注动态；查看不接管、不结案，也不表示通知已送达。"}


def configure(conn, *, owner_id, source_kind, source_key, enabled, expected_revision,
              request_id, repositories=(), now=None):
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 512:
        raise ValueError("operator required")
    if not isinstance(source_key, str) or not source_key or len(source_key) > 500:
        raise ValueError("invalid source key")
    if source_kind not in ("release", "case") or type(enabled) is not bool:
        raise ValueError("invalid subscription")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("invalid revision")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("invalid request")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    binding = digest([owner_id, source_kind, source_key, enabled, expected_revision])
    sid = "watch_" + digest([owner_id, source_kind, source_key])[:32]
    with transaction(conn):
        previous = conn.execute("SELECT * FROM watch_subscription_history WHERE request_id=?", (request_id,)).fetchone()
        if previous:
            if previous["request_digest"] != binding:
                raise ValueError("request binding changed")
            return json.loads(previous["result_json"])
        row = conn.execute("SELECT * FROM watch_subscriptions WHERE subscription_id=?", (sid,)).fetchone()
        if (row["revision"] if row else 0) != expected_revision:
            raise ValueError("subscription changed; reread")
        # Disabling a removed repository must remain possible; enabling needs a valid target.
        if enabled:
            if source_kind == "release" and source_key not in repositories:
                raise ValueError("repository not configured")
            if source_kind == "case" and not conn.execute("SELECT 1 FROM cases WHERE case_id=?", (source_key,)).fetchone():
                raise ValueError("case not found")
        conn.execute(
            """INSERT INTO watch_subscriptions VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(subscription_id) DO UPDATE SET revision=excluded.revision,
               enabled=excluded.enabled,updated_at=excluded.updated_at""",
            (sid, owner_id, source_kind, source_key, expected_revision + 1, int(enabled), now.isoformat(), now.isoformat()),
        )
        result = dict(conn.execute("SELECT * FROM watch_subscriptions WHERE subscription_id=?", (sid,)).fetchone())
        conn.execute("INSERT INTO watch_subscription_history VALUES(?,?,?)", (request_id, binding, canonical_json(result)))
        return result


def collect_releases(conn, *, owner_id, limit=100, now=None):
    return _collect(conn, owner_id=owner_id, source_kind="release", limit=limit, now=now)


def collect_cases(conn, *, owner_id, limit=100, now=None):
    """Collect actual recorded transitions, never infer events from current status."""
    return _collect(conn, owner_id=owner_id, source_kind="case", limit=limit, now=now)


def _collect(conn, *, owner_id, source_kind, limit, now):
    if not isinstance(owner_id, str) or not owner_id.strip() or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid owner or limit")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone required")
    table, key, identity = {
        "release": ("release_impacts", "repository", "impact_id"),
        "case": ("case_events", "case_id", "event_id"),
    }[source_kind]
    with (nullcontext(conn) if conn.in_transaction else transaction(conn)):
        rows = conn.execute(
            f"""SELECT s.subscription_id,r.{identity} FROM watch_subscriptions s
               JOIN {table} r ON r.{key}=s.source_key
               WHERE s.source_kind=? AND s.owner_id=? AND s.enabled=1
                 AND julianday(r.created_at)>julianday(s.created_at)
                 AND julianday(r.created_at)<=julianday(?)
                 AND NOT EXISTS(SELECT 1 FROM watch_actions a
                     WHERE a.subscription_id=s.subscription_id AND a.source_id=r.{identity})
               ORDER BY r.created_at,s.subscription_id,r.{identity} LIMIT ?""",
            (source_kind, owner_id, now.isoformat(), limit),
        ).fetchall()
        for row in rows:
            conn.execute("INSERT INTO watch_actions VALUES(?,?,?,?)",
                         ("watchact_" + digest(list(row))[:32], *row, now.isoformat()))
    return {"created": len(rows), "external_messages_sent": 0}
