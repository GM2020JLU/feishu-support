"""Exact, expiring Bug scopes managed exclusively by authenticated controls.

This module answers only whether a stored grant covers an action. Runtime mode,
remote permissions, resource ownership, action-specific approval and evidence
checks remain mandatory at dispatch. A positive answer is not an execution token.
No wildcards, implicit defaults, dynamic queries or substring path matching.
"""

import json
from pathlib import PurePosixPath

from .db import atomic, transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _bug, _text
from .timeutil import iso_now, parse_iso, utc_now

ACTIONS = {
    "bug.read",
    "bug.create",
    "bug.fields",
    "bug.comment",
    "bug.transition",
    "bug.close",
    "code.read",
    "code.edit",
    "code.build",
    "code.test",
    "code.commit",
    "code.push",
    "code.merge",
    "device.read",
    "device.reset",
    "device.ram_boot",
    "device.flash",
}
SCOPE_KEYS = {
    "host",
    "project_key",
    "type_key",
    "bug_ids",
    "actions",
    "fields",
    "transitions",
    "repositories",
    "devices",
}
TARGET_KEYS = {
    "host",
    "project_key",
    "type_key",
    "bug_id",
    "action",
    "fields",
    "transition",
    "repository",
    "device",
}


def _strings(value, label, *, nonempty=False):
    if not isinstance(value, list) or len(value) > 500 or (nonempty and not value):
        raise ValueError(f"invalid {label}")
    for item in value:
        _text(item, label)
        if item == "*":
            raise ValueError("wildcard grants are not supported")
    if len(set(value)) != len(value):
        raise ValueError(f"duplicate {label}")


def _path(value):
    _text(value, "repository path", 2000)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("repository paths must be normalized relative POSIX paths")
    return path


def validate_scope(scope):
    if not isinstance(scope, dict) or set(scope) != SCOPE_KEYS:
        raise ValueError("grant requires exact scope dimensions")
    for key in ("host", "project_key", "type_key"):
        _text(scope[key], key)
    for key in ("bug_ids", "actions", "fields", "transitions"):
        _strings(scope[key], key, nonempty=key in {"bug_ids", "actions"})
    if not set(scope["actions"]) <= ACTIONS:
        raise ValueError("unsupported grant action")
    for key in ("repositories", "devices"):
        if not isinstance(scope[key], list) or len(scope[key]) > 100:
            raise ValueError(f"invalid {key}")
    for repo in scope["repositories"]:
        if not isinstance(repo, dict) or set(repo) != {
            "name",
            "node",
            "branches",
            "paths",
        }:
            raise ValueError("repository grant requires name, node, branches and paths")
        _text(repo["name"], "repository name")
        _text(repo["node"], "node")
        _strings(repo["branches"], "branches", nonempty=True)
        _strings(repo["paths"], "paths", nonempty=True)
        for path in repo["paths"]:
            _path(path)
    for device in scope["devices"]:
        if not isinstance(device, dict) or set(device) != {"id", "node"}:
            raise ValueError("device grant requires exact device and node")
        _text(device["id"], "device")
        _text(device["node"], "node")
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
            "SELECT * FROM project_bug_grants WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("grant request ID reused for different content")
            return dict(old)  # Replays cannot renew expired or revoked authority.
        if expiry <= utc_now():
            raise ValueError("grant must expire in the future")
        for bug_id in scope["bug_ids"]:
            bug = conn.execute(
                "SELECT * FROM project_bugs WHERE bug_id=?", (bug_id,)
            ).fetchone()
            if bug is None or any(
                bug[key] != scope[key] for key in ("host", "project_key", "type_key")
            ):
                raise ValueError("grant Bug does not match its space and type")
        grant_id = new_id("pbg")
        conn.execute(
            "INSERT INTO project_bug_grants VALUES(?,?,?,?,?,?,NULL,?)",
            (
                grant_id,
                actor,
                request_id,
                signature,
                canonical_json(scope),
                expires_at,
                iso_now(),
            ),
        )
        conn.execute(
            "INSERT INTO project_bug_grant_events VALUES(?,?,?,?,?)",
            (new_id("pbge"), grant_id, actor, "issued", iso_now()),
        )
        return dict(
            conn.execute(
                "SELECT * FROM project_bug_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
        )


def revoke(conn, *, grant_id, actor):
    with transaction(conn):
        grant = conn.execute(
            "SELECT * FROM project_bug_grants WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if grant is None or grant["actor"] != actor:
            raise ValueError("grant is not owned by this operator")
        if grant["revoked_at"] is None:
            conn.execute(
                "UPDATE project_bug_grants SET revoked_at=? WHERE grant_id=?",
                (iso_now(), grant_id),
            )
            conn.execute(
                "INSERT INTO project_bug_grant_events VALUES(?,?,?,?,?)",
                (new_id("pbge"), grant_id, actor, "revoked", iso_now()),
            )


def projection(row, *, now=None):
    """Safe control view. Active scope is not evidence of dispatch permission."""
    now = now or utc_now()
    status = (
        "revoked"
        if row["revoked_at"]
        else "expired"
        if parse_iso(row["expires_at"]) <= now
        else "active"
    )
    return {
        "grant_id": row["grant_id"],
        "scope": json.loads(row["scope_json"]),
        "status": status,
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "created_at": row["created_at"],
        "can_revoke": row["revoked_at"] is None,
    }


def list_for_bug(conn, *, bug_id, actor, after_id):
    """Only grants owned by this operator, with bounded keyset pagination."""
    _text(bug_id, "Bug ID")
    _text(actor, "actor")
    if not isinstance(after_id, str) or len(after_id) > 256:
        raise ValueError("invalid grant cursor")
    _bug(conn, bug_id)
    rows = conn.execute(
        """SELECT g.* FROM project_bug_grants g WHERE actor=? AND grant_id>?
        AND EXISTS (SELECT 1 FROM json_each(g.scope_json,'$.bug_ids') WHERE value=?)
        ORDER BY grant_id LIMIT 31""",
        (actor, after_id, bug_id),
    ).fetchall()
    now = utc_now()
    return {
        "items": [projection(row, now=now) for row in rows[:30]],
        "next_cursor": rows[29]["grant_id"] if len(rows) > 30 else None,
    }


def _target(target):
    if not isinstance(target, dict) or set(target) != TARGET_KEYS:
        raise ValueError("action requires exact scope dimensions")
    for key in ("host", "project_key", "type_key", "bug_id", "action"):
        _text(target[key], key)
    if target["action"] not in ACTIONS:
        raise ValueError("unsupported action")
    _strings(target["fields"], "fields")
    action = target["action"]
    if action == "bug.fields":
        if not target["fields"]:
            raise ValueError("field update requires field keys")
    elif target["fields"]:
        raise ValueError("fields are only valid for field updates")
    if action in {"bug.transition", "bug.close"}:
        _text(target["transition"], "transition")
    elif target["transition"] is not None:
        raise ValueError("unexpected transition")
    repo = target["repository"]
    if action.startswith("code."):
        if not isinstance(repo, dict) or set(repo) != {
            "name",
            "node",
            "branch",
            "paths",
        }:
            raise ValueError("code action requires exact repository scope")
        for key in ("name", "node", "branch"):
            _text(repo[key], key)
        _strings(repo["paths"], "paths", nonempty=True)
        for path in repo["paths"]:
            _path(path)
    elif repo is not None:
        raise ValueError("unexpected repository")
    device = target["device"]
    if action.startswith("device."):
        if not isinstance(device, dict) or set(device) != {"id", "node"}:
            raise ValueError("device action requires exact device and node")
        for value in device.values():
            _text(value, "device identity")
    elif device is not None:
        raise ValueError("unexpected device")


def covers(conn, *, grant_id, actor, target):
    """Check current scope; dispatch must recheck within its claim transaction."""
    _target(target)
    row = conn.execute(
        "SELECT * FROM project_bug_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    if (
        row is None
        or row["actor"] != actor
        or row["revoked_at"]
        or parse_iso(row["expires_at"]) <= utc_now()
    ):
        return False
    scope = json.loads(row["scope_json"])
    if any(target[key] != scope[key] for key in ("host", "project_key", "type_key")):
        return False
    if (
        target["bug_id"] not in scope["bug_ids"]
        or target["action"] not in scope["actions"]
    ):
        return False
    if not set(target["fields"]) <= set(scope["fields"]):
        return False
    if (
        target["transition"] is not None
        and target["transition"] not in scope["transitions"]
    ):
        return False
    repo = target["repository"]
    if repo is not None:
        matches = [
            r
            for r in scope["repositories"]
            if r["name"] == repo["name"]
            and r["node"] == repo["node"]
            and repo["branch"] in r["branches"]
        ]
        if not any(
            all(
                any(_path(p).is_relative_to(_path(root)) for root in r["paths"])
                for p in repo["paths"]
            )
            for r in matches
        ):
            return False
    return target["device"] is None or target["device"] in scope["devices"]


def require_bug_read(conn, bug, *, actor, grant_id):
    target = {k: bug[k] for k in ("host", "project_key", "type_key", "bug_id")}
    target.update(
        action="bug.read", fields=[], transition=None, repository=None, device=None
    )
    if not covers(conn, grant_id=grant_id, actor=actor, target=target):
        raise PermissionError("Bug read grant is absent, expired or revoked")
