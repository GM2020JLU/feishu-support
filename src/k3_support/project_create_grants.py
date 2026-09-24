"""Exact, bounded Project creation grants managed only by authenticated controls.

This module only records whether a trusted operator may custody creation drafts
for one Project space/type. It never creates Project items and never broadens
runtime, remote permission, duplicate, or required-field checks.
"""

import sqlite3

from .db import atomic, transaction
from .ids import digest, new_id
from .project_bugs import BugConflict, _text
from .timeutil import iso_now, parse_iso, utc_now

SCOPE_KEYS = {"host", "project_key", "type_key", "max_creations"}
BUDGET_STATES = {"dispatched", "unknown", "created"}


def validate_scope(scope):
    if not isinstance(scope, dict) or set(scope) != SCOPE_KEYS:
        raise ValueError("create grant requires exact scope dimensions")
    for key in ("host", "project_key", "type_key"):
        _text(scope[key], key)
        if scope[key] == "*":
            raise ValueError("wildcard grants are not supported")
    if type(scope["max_creations"]) is not int or not 1 <= scope["max_creations"] <= 20:
        raise ValueError("invalid max creations")
    return scope


def issue(conn, *, actor, request_id, scope, expires_at):
    """Called by a trusted authenticated operator entry, never a worker RPC."""
    _text(actor, "actor")
    _text(request_id, "request ID")
    validate_scope(scope)
    expiry = parse_iso(expires_at)
    signature = digest({"scope": scope, "expires_at": expires_at})
    with atomic(conn):
        old = conn.execute(
            "SELECT * FROM project_create_grants WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("create grant request ID reused for different content")
            return dict(old)
        if expiry <= utc_now():
            raise ValueError("create grant must expire in the future")
        grant_id = new_id("pcg")
        now = iso_now()
        conn.execute(
            """INSERT INTO project_create_grants
            (grant_id,actor,request_id,request_digest,host,project_key,type_key,
             max_creations,expires_at,revoked_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,NULL,?)""",
            (
                grant_id,
                actor,
                request_id,
                signature,
                scope["host"],
                scope["project_key"],
                scope["type_key"],
                scope["max_creations"],
                expires_at,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO project_create_grant_events VALUES(?,?,?,?,?)",
            (new_id("pcge"), grant_id, actor, "issued", iso_now()),
        )
        return dict(
            conn.execute(
                "SELECT * FROM project_create_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
        )


def revoke(conn, *, grant_id, actor):
    """Owner revocation is final and idempotent for exact replays."""
    _text(grant_id, "create grant")
    _text(actor, "actor")
    with transaction(conn):
        grant = conn.execute(
            "SELECT * FROM project_create_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if grant is None or grant["actor"] != actor:
            raise ValueError("create grant is not owned by this operator")
        if grant["revoked_at"] is None:
            conn.execute(
                "UPDATE project_create_grants SET revoked_at=? WHERE grant_id=?",
                (iso_now(), grant_id),
            )
            conn.execute(
                "INSERT INTO project_create_grant_events VALUES(?,?,?,?,?)",
                (new_id("pcge"), grant_id, actor, "revoked", iso_now()),
            )


def _row_value(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def projection(row, *, now=None):
    """Safe control view; active budget is not evidence of dispatch permission."""
    now = now or utc_now()
    status = (
        "revoked"
        if row["revoked_at"]
        else "expired"
        if parse_iso(row["expires_at"]) <= now
        else "active"
    )
    used = int(_row_value(row, "used", 0) or 0)
    remaining = max(0, row["max_creations"] - used)
    return {
        "grant_id": row["grant_id"],
        "scope": {
            "host": row["host"],
            "project_key": row["project_key"],
            "type_key": row["type_key"],
            "max_creations": row["max_creations"],
        },
        "status": status,
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "created_at": row["created_at"],
        "can_revoke": row["revoked_at"] is None,
        "used": used,
        "remaining": remaining,
    }


def list_for_actor(conn, *, actor, after_id):
    """Only grants owned by this operator, with bounded keyset pagination."""
    _text(actor, "actor")
    if not isinstance(after_id, str) or len(after_id) > 256:
        raise ValueError("invalid create grant cursor")
    rows = conn.execute(
        """SELECT g.*,
        (SELECT count(*) FROM project_bug_create_drafts d
         WHERE d.grant_id=g.grant_id AND d.state IN ('dispatched','unknown','created')) AS used
        FROM project_create_grants g WHERE actor=? AND grant_id>?
        ORDER BY grant_id LIMIT 31""",
        (actor, after_id),
    ).fetchall()
    now = utc_now()
    return {
        "items": [projection(row, now=now) for row in rows[:30]],
        "next_cursor": rows[29]["grant_id"] if len(rows) > 30 else None,
    }


def remaining_budget(conn, grant_id):
    """Return unconsumed create budget; unsettled dispatches remain consumed."""
    _text(grant_id, "create grant")
    grant = conn.execute(
        "SELECT max_creations FROM project_create_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    if grant is None:
        return 0
    try:
        used = conn.execute(
            """SELECT count(*) FROM project_bug_create_drafts
            WHERE grant_id=? AND state IN ('dispatched','unknown','created')""",
            (grant_id,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    return max(0, grant["max_creations"] - used)


def covers(conn, *, grant_id, actor, host, project_key, type_key):
    """Check current scope; draft dispatch must recheck inside reservation."""
    for label, value in (
        ("create grant", grant_id),
        ("actor", actor),
        ("host", host),
        ("project key", project_key),
        ("type key", type_key),
    ):
        _text(value, label)
    row = conn.execute(
        "SELECT * FROM project_create_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    return not (
        row is None
        or row["actor"] != actor
        or row["revoked_at"]
        or parse_iso(row["expires_at"]) <= utc_now()
        or row["host"] != host
        or row["project_key"] != project_key
        or row["type_key"] != type_key
        or remaining_budget(conn, grant_id) <= 0
    )
