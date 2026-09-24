"""Durable read-only refresh queue with lease/config/grant fences.

Expired reads can be reclaimed: no external mutations occur. Old consumers cannot
persist after losing their lease. Snapshot observation IDs reconcile crash gaps.
"""

import sqlite3
from datetime import timedelta

from . import project_bug_grants as grants
from . import project_bug_sync as sync
from . import project_bugs as bugs
from .db import transaction
from .ids import digest, new_id
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_reader_config import selected
from .runtime_control import capability_allowed, current_global_state
from .timeutil import iso_now, parse_iso, utc_now


class RefreshBlocked(ValueError):
    pass


def projection(row):
    result = {
        k: row[k]
        for k in (
            "refresh_id",
            "bug_id",
            "grant_id",
            "state",
            "attempt",
            "snapshot_id",
            "error_code",
            "created_at",
            "updated_at",
        )
    }
    result["lease_expired"] = (
        row["state"] == "running" and parse_iso(row["lease_expires_at"]) <= utc_now()
    )
    return result


def _selection(conn, config, bug, actor):
    reader = selected(config)
    if (
        reader is None
        or reader["host"] != bug["host"]
        or actor != config.control_operator_id
        or config.mode == "drain"
        or not capability_allowed(conn, config, "retrieve")
    ):
        raise RefreshBlocked("reader_disabled_or_policy_changed")
    return reader


def _fingerprint(conn, config, reader):
    return digest(
        {
            "reader": reader,
            "static_mode": config.mode,
            "control_revision": current_global_state(conn, config)["revision"],
        }
    )


def enqueue(conn, config, *, bug_id, actor, grant_id, request_id):
    bugs._text(request_id, "refresh request", 256)
    signature = digest({"bug_id": bug_id, "grant_id": grant_id})
    with transaction(conn):
        bug = bugs._bug(conn, bug_id)
        grants.require_bug_read(conn, bug, actor=actor, grant_id=grant_id)
        old = conn.execute(
            "SELECT * FROM project_refresh_requests WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise bugs.BugConflict("refresh request ID reused for different intent")
            return projection(old)
        reader = _selection(conn, config, bug, actor)
        if conn.execute(
            "SELECT 1 FROM project_refresh_requests WHERE bug_id=? AND state IN ('queued','running')",
            (bug_id,),
        ).fetchone():
            raise bugs.BugConflict("Bug already has a queued or running refresh")
        key, now = new_id("refresh"), iso_now()
        conn.execute(
            "INSERT INTO project_refresh_requests(refresh_id,bug_id,actor,request_id,grant_id,request_digest,reader_digest,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
            (
                key,
                bug_id,
                actor,
                request_id,
                grant_id,
                signature,
                _fingerprint(conn, config, reader),
                now,
                now,
            ),
        )
        bugs._event(conn, bug_id, actor, "refresh_queued", {"refresh_id": key})
        return projection(
            conn.execute(
                "SELECT * FROM project_refresh_requests WHERE refresh_id=?", (key,)
            ).fetchone()
        )


def status(conn, *, refresh_id, actor):
    row = conn.execute(
        "SELECT * FROM project_refresh_requests WHERE refresh_id=? AND actor=?",
        (refresh_id, actor),
    ).fetchone()
    if row is None:
        raise ValueError("refresh request not found")
    return projection(row)


def controls(conn, config, bug, actor):
    try:
        _selection(conn, config, bug, actor)
        available = True
    except RefreshBlocked:
        available = False
    choices = []
    for row in conn.execute(
        "SELECT grant_id,expires_at FROM project_bug_grants WHERE actor=? AND revoked_at IS NULL ORDER BY created_at DESC,grant_id DESC LIMIT 200",
        (actor,),
    ):
        try:
            grants.require_bug_read(conn, bug, actor=actor, grant_id=row["grant_id"])
        except PermissionError:
            continue
        choices.append(dict(row))
        if len(choices) == 20:
            break
    return {
        "available": available,
        "grants": choices,
        "grant_limit": 20,
        "requests": [
            projection(r)
            for r in conn.execute(
                "SELECT * FROM project_refresh_requests WHERE bug_id=? AND actor=? ORDER BY rowid DESC LIMIT 20",
                (bug["bug_id"], actor),
            )
        ],
        "history_limit": 20,
    }


def _finish(conn, row, state, *, error=None, snapshot_id=None):
    conn.execute(
        "UPDATE project_refresh_requests SET state=?,error_code=?,snapshot_id=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE refresh_id=?",
        (state, error, snapshot_id, iso_now(), row["refresh_id"]),
    )
    bugs._event(
        conn,
        row["bug_id"],
        row["actor"],
        "refresh_" + state,
        {
            "refresh_id": row["refresh_id"],
            "error_code": error,
            "snapshot_id": snapshot_id,
        },
    )


def run_one(conn, config_loader, *, client_factory=None, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return {"state": "stopped"}
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM project_refresh_requests WHERE state='queued' OR (state='running' AND lease_expires_at<=?) ORDER BY created_at,refresh_id LIMIT 1",
            (iso_now(),),
        ).fetchone()
        if row is None:
            return {"state": "idle"}
        # A prior process may have committed the immutable observation and exited
        # before updating queue status. Reconcile by exact observation identity.
        saved = conn.execute(
            "SELECT s.snapshot_id FROM project_bug_snapshots s JOIN project_bug_read_evidence e USING(snapshot_id) WHERE s.bug_id=? AND s.observation_id=? AND e.actor=? AND e.grant_id=?",
            (
                row["bug_id"],
                "refresh:" + row["refresh_id"],
                row["actor"],
                row["grant_id"],
            ),
        ).fetchone()
        if saved:
            _finish(conn, row, "succeeded", snapshot_id=saved[0])
            return status(conn, refresh_id=row["refresh_id"], actor=row["actor"])
        if row["attempt"] >= 3:
            _finish(conn, row, "failed", error="interrupted_read_limit")
            return status(conn, refresh_id=row["refresh_id"], actor=row["actor"])
        token = new_id("readlease")
        conn.execute(
            "UPDATE project_refresh_requests SET state='running',attempt=attempt+1,lease_token=?,lease_expires_at=?,updated_at=? WHERE refresh_id=?",
            (
                token,
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["refresh_id"],
            ),
        )
        bugs._event(
            conn,
            row["bug_id"],
            row["actor"],
            "refresh_started",
            {"refresh_id": row["refresh_id"], "attempt": row["attempt"] + 1},
        )

    def guard():
        current = conn.execute(
            "SELECT * FROM project_refresh_requests WHERE refresh_id=?",
            (row["refresh_id"],),
        ).fetchone()
        if (
            current is None
            or current["state"] != "running"
            or current["lease_token"] != token
            or parse_iso(current["lease_expires_at"]) <= utc_now()
        ):
            raise RefreshBlocked("read_lease_lost")
        if stop_event is not None and stop_event.is_set():
            raise RefreshBlocked("reader_stopped")
        config = config_loader()
        bug = bugs._bug(conn, row["bug_id"])
        reader = _selection(conn, config, bug, row["actor"])
        if _fingerprint(conn, config, reader) != row["reader_digest"]:
            raise RefreshBlocked("reader_configuration_changed")
        grants.require_bug_read(conn, bug, actor=row["actor"], grant_id=row["grant_id"])
        changed = conn.execute(
            "UPDATE project_refresh_requests SET lease_expires_at=?,updated_at=? WHERE refresh_id=? AND state='running' AND lease_token=? AND lease_expires_at>?",
            (
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["refresh_id"],
                token,
                iso_now(),
            ),
        )
        if changed.rowcount != 1:
            raise RefreshBlocked("read_lease_lost")
        return reader

    try:
        reader = guard()
        client = (
            client_factory
            or (
                lambda r: MeegleReadClient(
                    **{k: v for k, v in r.items() if k != "enabled"}
                )
            )
        )(reader)
        result = sync.refresh(
            conn,
            client,
            bug_id=row["bug_id"],
            actor=row["actor"],
            grant_id=row["grant_id"],
            observation_id="refresh:" + row["refresh_id"],
            guard=guard,
        )
        state, error, snapshot = "succeeded", None, result["snapshot_id"]
    except (PermissionError, RefreshBlocked):
        state, error, snapshot = "blocked", "authorization_or_reader_changed", None
    except bugs.BugConflict:
        state, error, snapshot = "failed", "newer_snapshot_or_conflict", None
    except ProjectReadError as exc:
        state, snapshot = "failed", None
        error = {
            "auth_login_required": "login_required",
            "auth_rejected": "login_required",
            "read_timeout": "read_timeout",
            "snapshot_changed_during_read": "remote_changed",
        }.get(exc.code, "project_read_failed")
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        state, error, snapshot = "failed", "reader_unavailable", None
    with transaction(conn):
        current = conn.execute(
            "SELECT * FROM project_refresh_requests WHERE refresh_id=?",
            (row["refresh_id"],),
        ).fetchone()
        if current["state"] == "running" and current["lease_token"] == token:
            _finish(conn, current, state, error=error, snapshot_id=snapshot)
    return status(conn, refresh_id=row["refresh_id"], actor=row["actor"])
