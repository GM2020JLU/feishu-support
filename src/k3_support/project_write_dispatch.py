"""Approved-scope field/transition dispatch queue, separate from read services.

Same custody discipline as the comment queue: one pending dispatch per operation,
leases with guarded renewal, a pinned dedicated writer identity, and blocked
rather than failed when authority, configuration or contracts are missing.
"""

import json
import sqlite3
from datetime import timedelta

from . import project_bug_operations as operations
from . import project_field_writer_config as policy
from .db import transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _event, _text
from .project_comment_reconcile import reader_identity
from .project_read_client import ProjectReadError
from .project_reader_config import selected
from .project_write_transport import WRITE_ACTIONS, WriteClient, WriteTransport
from .timeutil import iso_now, parse_iso, utc_now


class DispatchBlocked(ValueError):
    pass


def _owned(conn, actor, operation_id):
    _text(operation_id, "write operation", 256)
    op = operations._operation(conn, operation_id)
    if op["actor"] != actor or op["action"] not in WRITE_ACTIONS:
        raise ValueError("owned field or transition operation required")
    return op


def _context(conn, config, op):
    writer = config.raw.get("project_integration", {}).get("field_writer")
    if writer is None or not policy.validate(writer)["enabled"]:
        raise DispatchBlocked("writer_not_configured")
    reader = selected(config)
    if reader is None:
        raise DispatchBlocked("reader_unavailable")
    if writer["risk_policy"] != "recheck_window":
        raise DispatchBlocked("risk_policy_forbids_dispatch")
    if op["action"] == "bug.fields":
        if not policy.accepted_update(reader):
            raise DispatchBlocked("native_contract_unverified")
    elif not policy.accepted_transition(reader):
        raise DispatchBlocked("native_contract_unverified")
    bug, fence = operations._guard(conn, config, op)
    if reader["host"] != bug["host"]:
        raise DispatchBlocked("reader_destination_mismatch")
    return reader, writer, fence


def projection(row):
    result = {
        k: row[k]
        for k in (
            "dispatch_id",
            "operation_id",
            "state",
            "attempt",
            "error_code",
            "created_at",
            "updated_at",
        )
    }
    result["lease_expired"] = (
        row["state"] == "running" and parse_iso(row["lease_expires_at"]) <= utc_now()
    )
    result["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    return result


def _request(conn, actor, dispatch_id):
    _text(dispatch_id, "dispatch ID", 256)
    row = conn.execute(
        "SELECT * FROM project_write_dispatch_requests WHERE actor=? AND dispatch_id=?",
        (actor, dispatch_id),
    ).fetchone()
    if row is None:
        raise ValueError("write dispatch not found")
    return row


def status(conn, *, actor, dispatch_id):
    return projection(_request(conn, actor, dispatch_id))


def controls(conn, config, *, actor, operation_id):
    op = _owned(conn, actor, operation_id)
    available, reason = False, "operation_not_prepared"
    if op["state"] == "prepared":
        try:
            _context(conn, config, op)
            available, reason = True, None
        except DispatchBlocked as exc:
            reason = str(exc)
        except (ValueError, PermissionError, RuntimeError):
            reason = "authority_or_source_changed"
    rows = conn.execute(
        "SELECT * FROM project_write_dispatch_requests WHERE operation_id=? AND actor=? ORDER BY rowid DESC LIMIT 20",
        (operation_id, actor),
    ).fetchall()
    return {
        "available": available,
        "reason": reason,
        "requests": [projection(r) for r in rows],
    }


def enqueue(conn, config, *, actor, operation_id, expected_digest, request_id):
    _text(request_id, "dispatch request", 256)
    signature = digest(
        {"operation_id": operation_id, "expected_digest": expected_digest}
    )
    with transaction(conn):
        op = _owned(conn, actor, operation_id)
        old = conn.execute(
            "SELECT * FROM project_write_dispatch_requests WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("dispatch request reused for different intent")
            return projection(old)
        if op["state"] != "prepared" or op["request_digest"] != expected_digest:
            raise BugConflict("write changed or was already dispatched")
        reader, _, fence = _context(conn, config, op)
        if conn.execute(
            "SELECT 1 FROM project_write_dispatch_requests WHERE operation_id=? AND state IN ('queued','running')",
            (operation_id,),
        ).fetchone():
            raise BugConflict("write already has a pending dispatch")
        key, now = new_id("pwd"), iso_now()
        conn.execute(
            "INSERT INTO project_write_dispatch_requests(dispatch_id,operation_id,actor,request_id,request_digest,runtime_digest,reader_digest,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'queued',?,?)",
            (
                key,
                operation_id,
                actor,
                request_id,
                signature,
                fence,
                reader_identity(reader),
                now,
                now,
            ),
        )
        _event(
            conn,
            op["bug_id"],
            actor,
            "write_dispatch_queued",
            {"dispatch_id": key, "operation_id": operation_id},
        )
        return status(conn, actor=actor, dispatch_id=key)


def _finish(conn, row, state, *, result=None, error=None):
    conn.execute(
        "UPDATE project_write_dispatch_requests SET state=?,result_json=?,error_code=?,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE dispatch_id=?",
        (
            state,
            canonical_json(result) if result is not None else None,
            error,
            iso_now(),
            row["dispatch_id"],
        ),
    )


class _PinnedWriter:
    def __init__(self, client, user_key):
        self.client, self.user_key, self.host = client, user_key, client.host

    def read_page(self, *args, **kwargs):
        return self.client.read_page(*args, **kwargs)

    def query_bugs(self, *args, **kwargs):
        return self.client.query_bugs(*args, **kwargs)

    def check_identity(self):
        envelope = self.read_page("user.me", {})
        if (
            envelope.get("host") != self.host
            or envelope.get("command") != "user.me"
            or not isinstance(envelope.get("payload"), dict)
            or envelope["payload"].get("user_key") != self.user_key
        ):
            raise DispatchBlocked("writer_identity_changed")

    def update_fields(self, destination, fields):
        self.check_identity()
        return self.client.update_fields(destination, fields)

    def transition_state(self, destination, change):
        self.check_identity()
        return self.client.transition_state(destination, change)


def run_one(conn, config_loader, *, client_factory=None, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return {"state": "stopped"}
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM project_write_dispatch_requests WHERE state='queued' OR (state='running' AND lease_expires_at<=?) ORDER BY created_at,dispatch_id LIMIT 1",
            (iso_now(),),
        ).fetchone()
        if row is None:
            return {"state": "idle"}
        op = _owned(conn, row["actor"], row["operation_id"])
        if op["state"] != "prepared":
            # Recovery of an uncertain side effect is always read-only. Leave
            # actual reconciliation to the existing scoped reader queue.
            _finish(
                conn,
                row,
                "succeeded",
                result={
                    "operation_state": op["state"],
                    "dispatch_started_here": False,
                    "reconcile_required": op["state"] in {"dispatched", "unknown"},
                },
            )
            return status(conn, actor=row["actor"], dispatch_id=row["dispatch_id"])
        if row["attempt"] >= 3:
            _finish(conn, row, "failed", error="interrupted_dispatch_limit")
            return status(conn, actor=row["actor"], dispatch_id=row["dispatch_id"])
        token = new_id("pwdlease")
        conn.execute(
            "UPDATE project_write_dispatch_requests SET state='running',attempt=attempt+1,lease_token=?,lease_expires_at=?,updated_at=? WHERE dispatch_id=?",
            (
                token,
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["dispatch_id"],
            ),
        )

    def guard():
        current = _request(conn, row["actor"], row["dispatch_id"])
        if (
            current["state"] != "running"
            or current["lease_token"] != token
            or parse_iso(current["lease_expires_at"]) <= utc_now()
            or (stop_event is not None and stop_event.is_set())
        ):
            raise DispatchBlocked("dispatch_lease_lost")
        config = config_loader()
        operation = _owned(conn, row["actor"], row["operation_id"])
        reader, writer, fence = _context(conn, config, operation)
        if (
            fence != row["runtime_digest"]
            or reader_identity(reader) != row["reader_digest"]
        ):
            raise DispatchBlocked("dispatch_configuration_changed")
        conn.execute(
            "UPDATE project_write_dispatch_requests SET lease_expires_at=?,updated_at=? WHERE dispatch_id=? AND lease_token=?",
            (
                (utc_now() + timedelta(seconds=90)).isoformat(),
                iso_now(),
                row["dispatch_id"],
                token,
            ),
        )
        return config, reader, writer

    try:
        config, reader, writer = guard()
        native = (
            client_factory
            or (
                lambda r: WriteClient(**{k: v for k, v in r.items() if k != "enabled"})
            )
        )(reader)
        client = _PinnedWriter(native, writer["user_key"])
        client.check_identity()
        transport = WriteTransport(
            conn,
            client,
            config=config,
            reader_digest=row["reader_digest"],
            before_read=guard,
            allowed_fields=frozenset(writer["allowed_fields"]),
            risk_policy=writer["risk_policy"],
            closing_status_ids=frozenset(writer["closing_status_ids"]),
            update_contract_verified=policy.accepted_update(reader),
            transition_contract_verified=policy.accepted_transition(reader),
        )
        result = operations.dispatch(
            conn,
            config,
            operation_id=row["operation_id"],
            transport=transport,
            before_dispatch=guard,
            before_settle=guard,
            unknown_recheck=(5.0, 10.0),
        )
        with transaction(conn):
            guard()
            _finish(
                conn,
                row,
                "succeeded",
                result={
                    "operation_state": result["state"],
                    "dispatch_started_here": True,
                    "reconcile_required": result["state"] in {"dispatched", "unknown"},
                },
            )
        return status(conn, actor=row["actor"], dispatch_id=row["dispatch_id"])
    except DispatchBlocked as exc:
        state, error = "blocked", str(exc)
    except (PermissionError, BugConflict):
        state, error = "blocked", "authority_or_source_changed"
    except ProjectReadError:
        state, error = "failed", "write_provider_unavailable"
    except (ValueError, RuntimeError, OSError, sqlite3.Error):
        state, error = "failed", "write_dispatch_unavailable"
    with transaction(conn):
        current = _request(conn, row["actor"], row["dispatch_id"])
        if current["state"] == "running" and current["lease_token"] == token:
            _finish(conn, row, state, error=error)
    return status(conn, actor=row["actor"], dispatch_id=row["dispatch_id"])
