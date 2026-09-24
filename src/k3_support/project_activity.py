"""Durable comments and operation-activity observations under exact Bug read grants."""

import json
import sqlite3
from datetime import timedelta

from . import project_bug_grants as grants
from . import project_bugs as bugs
from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_attachments_reader import AttachmentsReader
from .project_comments_reader import CommentsReader
from .project_history_reader import HistoryReader
from .project_read_client import MeegleReadClient, ProjectReadError
from .project_refresh import RefreshBlocked, _fingerprint, _selection
from .project_relations_reader import RelationsReader
from .timeutil import iso_now, parse_iso, utc_now

PAGE_SIZE = 20


def projection(row):
    result = {
        k: row[k]
        for k in (
            "activity_id",
            "bug_id",
            "grant_id",
            "state",
            "attempt",
            "error_code",
            "end_time_ms",
            "kind",
            "created_at",
            "updated_at",
        )
    }
    result["lease_expired"] = (
        row["state"] == "running" and parse_iso(row["lease_expires_at"]) <= utc_now()
    )
    result["observation"] = (
        {k: v for k, v in json.loads(row["result_json"]).items() if k != "items"}
        if row["result_json"]
        else None
    )
    if row["kind"] in {"comment_reconcile", "write_reconcile"}:
        result["source"] = json.loads(row["source_json"])
    return result


def _get(conn, actor, activity_id):
    bugs._text(activity_id, "activity ID", 256)
    row = conn.execute(
        "SELECT * FROM project_activity_requests WHERE activity_id=? AND actor=?",
        (activity_id, actor),
    ).fetchone()
    if row is None:
        raise ValueError("activity not found")
    return row


def status(conn, *, actor, activity_id):
    return projection(_get(conn, actor, activity_id))


def page(conn, *, actor, activity_id, offset):
    if type(offset) is not int or not 0 <= offset <= 10000 or offset % PAGE_SIZE:
        raise ValueError("invalid activity page")
    row = _get(conn, actor, activity_id)
    if row["state"] != "succeeded":
        raise ValueError("activity observation is unavailable")
    result = json.loads(row["result_json"])
    if offset > len(result["items"]):
        raise ValueError("activity page is outside observation")
    return {
        "activity_id": activity_id,
        "bug_id": row["bug_id"],
        "kind": row["kind"],
        "offset": offset,
        "items": result["items"][offset : offset + PAGE_SIZE],
        "observation": projection(row)["observation"],
        "next_offset": offset + PAGE_SIZE
        if offset + PAGE_SIZE < len(result["items"])
        else None,
    }


def controls(conn, bug_id, actor, read_controls):
    return {
        "available": read_controls["available"],
        "grants": read_controls["grants"],
        "requests": [
            projection(r)
            for r in conn.execute(
                "SELECT * FROM project_activity_requests WHERE bug_id=? AND actor=? ORDER BY rowid DESC LIMIT 20",
                (bug_id, actor),
            )
        ],
        "activity_limit": 20,
    }


def enqueue(conn, config, *, actor, bug_id, grant_id, request_id, kind, source=None):
    if not isinstance(kind, str) or kind not in {
        "history",
        "comments",
        "relations",
        "attachments",
        "attachment_download",
        "comment_reconcile",
        "write_reconcile",
    }:
        raise ValueError("unsupported activity kind")
    if kind == "attachment_download":
        from .project_attachment_download import selection

        if selection(conn, actor, source)[0] != bug_id:
            raise ValueError("attachment source Bug mismatch")
    elif kind == "comment_reconcile":
        from .project_comment_reconcile import selection

        if selection(conn, actor, source)["bug_id"] != bug_id:
            raise ValueError("comment reconciliation Bug mismatch")
    elif kind == "write_reconcile":
        from .project_write_reconcile import selection

        if selection(conn, actor, source)["bug_id"] != bug_id:
            raise ValueError("write reconciliation Bug mismatch")
    elif source is not None:
        raise ValueError("unexpected attachment selection")
    bugs._text(request_id, "activity request", 256)
    signature = digest({"bug_id": bug_id, "grant_id": grant_id, "kind": kind})
    if source is not None:
        signature = digest({"request": signature, "source": source})
    with transaction(conn):
        bug = bugs._bug(conn, bug_id)
        old = conn.execute(
            "SELECT * FROM project_activity_requests WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise bugs.BugConflict("activity request reused for different intent")
            return projection(old)
        if kind in {"comment_reconcile", "write_reconcile"} and selection(
            conn, actor, source
        )["state"] not in {"dispatched", "unknown"}:
            raise ValueError("write does not need reconciliation")
        grants.require_bug_read(conn, bug, actor=actor, grant_id=grant_id)
        reader = _selection(conn, config, bug, actor)
        if conn.execute(
            "SELECT 1 FROM project_activity_requests WHERE bug_id=? AND kind=? AND state IN ('queued','running')",
            (bug_id, kind),
        ).fetchone():
            raise bugs.BugConflict("Bug already has a pending activity read")
        key, now = new_id("activity"), iso_now()
        columns_extra = ",source_json" if source is not None else ""
        values_extra = ",?" if source is not None else ""
        conn.execute(
            f"INSERT INTO project_activity_requests(activity_id,bug_id,actor,grant_id,request_id,request_digest,reader_digest,end_time_ms,kind,state,created_at,updated_at{columns_extra}) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?{values_extra})",
            (
                key,
                bug_id,
                actor,
                grant_id,
                request_id,
                signature,
                _fingerprint(conn, config, reader),
                int(utc_now().timestamp() * 1000),
                kind,
                now,
                now,
            )
            + ((canonical_json(source),) if source is not None else ()),
        )
        bugs._event(conn, bug_id, actor, "activity_queued", {"activity_id": key})
        return status(conn, actor=actor, activity_id=key)


def _finish(conn, row, state, *, error=None, result=None):
    conn.execute(
        "UPDATE project_activity_requests SET state=?,error_code=?,result_json=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE activity_id=?",
        (
            state,
            error,
            canonical_json(result) if result is not None else None,
            iso_now(),
            row["activity_id"],
        ),
    )
    bugs._event(
        conn,
        row["bug_id"],
        row["actor"],
        "activity_" + state,
        {"activity_id": row["activity_id"], "error_code": error},
    )


def run_one(conn, config_loader, *, client_factory=None, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return {"state": "stopped"}
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM project_activity_requests WHERE state='queued' OR (state='running' AND lease_expires_at<=?) ORDER BY created_at,activity_id LIMIT 1",
            (iso_now(),),
        ).fetchone()
        if row is None:
            return {"state": "idle"}
        if row["attempt"] >= 3:
            _finish(conn, row, "failed", error="interrupted_read_limit")
            return status(conn, actor=row["actor"], activity_id=row["activity_id"])
        token = new_id("activitylease")
        conn.execute(
            "UPDATE project_activity_requests SET state='running',attempt=attempt+1,lease_token=?,lease_expires_at=?,updated_at=? WHERE activity_id=?",
            (
                token,
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["activity_id"],
            ),
        )
        bugs._event(
            conn,
            row["bug_id"],
            row["actor"],
            "activity_started",
            {"activity_id": row["activity_id"], "attempt": row["attempt"] + 1},
        )

    def guard():
        current = _get(conn, row["actor"], row["activity_id"])
        if (
            current["state"] != "running"
            or current["lease_token"] != token
            or parse_iso(current["lease_expires_at"]) <= utc_now()
            or (stop_event is not None and stop_event.is_set())
        ):
            raise RefreshBlocked("activity_lease_lost")
        config = config_loader()
        bug = bugs._bug(conn, row["bug_id"])
        reader = _selection(conn, config, bug, row["actor"])
        if _fingerprint(conn, config, reader) != row["reader_digest"]:
            raise RefreshBlocked("activity_reader_changed")
        grants.require_bug_read(conn, bug, actor=row["actor"], grant_id=row["grant_id"])
        changed = conn.execute(
            "UPDATE project_activity_requests SET lease_expires_at=?,updated_at=? WHERE activity_id=? AND state='running' AND lease_token=? AND lease_expires_at>?",
            (
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["activity_id"],
                token,
                iso_now(),
            ),
        )
        if changed.rowcount != 1:
            raise RefreshBlocked("activity_lease_lost")
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
        bug = bugs._bug(conn, row["bug_id"])
        destination = {
            k: bug[k] for k in ("host", "project_key", "type_key", "item_id")
        }
        if row["kind"] == "attachment_download":
            from .project_attachment_download import collect

            config = config_loader()
            result = collect(
                client, config, destination, json.loads(row["source_json"]), guard=guard
            )
        elif row["kind"] in {"comment_reconcile", "write_reconcile"}:
            if row["kind"] == "comment_reconcile":
                from .project_comment_reconcile import collect
            else:
                from .project_write_reconcile import collect

            result = collect(
                conn,
                client,
                reader,
                actor=row["actor"],
                source=json.loads(row["source_json"]),
                guard=guard,
            )
        else:
            collector = {
                "history": HistoryReader,
                "comments": CommentsReader,
                "relations": RelationsReader,
                "attachments": AttachmentsReader,
            }[row["kind"]]
            result = collector(client, before_read=guard).collect(
                destination, end=row["end_time_ms"]
            )
        with transaction(conn):
            guard()
            _finish(conn, row, "succeeded", result=result)
        return status(conn, actor=row["actor"], activity_id=row["activity_id"])
    except (PermissionError, RefreshBlocked):
        state, error = "blocked", "authorization_or_reader_changed"
    except ProjectReadError as exc:
        state, error = (
            "failed",
            {
                "auth_login_required": "login_required",
                "auth_rejected": "login_required",
                "history_changed_during_read": "remote_changed",
                "snapshot_changed_during_read": "remote_changed",
                "attachment_source_changed": "remote_changed",
                "attachment_storage_full": "attachment_storage_full",
                "attachment_transfer_timeout": "read_timeout",
                "comments_changed_during_read": "remote_changed",
                "relations_changed_during_read": "remote_changed",
                "history_budget_exceeded": "activity_limit",
                "comments_budget_exceeded": "activity_limit",
                "relations_budget_exceeded": "activity_limit",
            }.get(exc.code, "activity_read_failed"),
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        state, error = "failed", "reader_unavailable"
    with transaction(conn):
        current = _get(conn, row["actor"], row["activity_id"])
        if current["state"] == "running" and current["lease_token"] == token:
            _finish(conn, current, state, error=error)
    return status(conn, actor=row["actor"], activity_id=row["activity_id"])
