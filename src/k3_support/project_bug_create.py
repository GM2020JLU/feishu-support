"""Durable Project bug creation draft custody with no native transport effects.

Trusted controls may prepare, validate, reserve, and settle creation intent here.
This module never calls Feishu Project, never bypasses duplicate or required-field
checks, and treats dispatched or unknown custody as possibly side-effecting.
"""

import json
import re

from . import project_create_grants
from .db import atomic, transaction
from .ids import canonical_json, digest, new_id
from .project_bugs import BugConflict, _text
from .project_create_schema import empty
from .timeutil import iso_now

DRAFT_STATES = {"draft", "ready"}
IN_FLIGHT_STATES = {"dispatched", "unknown"}
TERMINAL_STATES = {"created", "rejected", "cancelled"}
REJECT_ERROR_CODES = {
    "provider_rejected",
    "permission_denied",
    "contract_mismatch",
    "operator_verified_absent",
}
ITEM_ID_RE = re.compile(r"[1-9][0-9]{0,63}")


def _json(value, label):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    return canonical_json(value)


def _field_values(field_values):
    if not isinstance(field_values, dict) or not field_values:
        raise ValueError("field values must be a nonempty object")
    for key in field_values:
        _text(key, "field key")
    encoded = _json(field_values, "field values")
    if len(encoded.encode("utf-8")) > 256000:
        raise ValueError("field values are too large")
    return encoded


def _required_fields(required_fields):
    if not isinstance(required_fields, list) or len(required_fields) > 200:
        raise ValueError("invalid required fields")
    for entry in required_fields:
        if not isinstance(entry, dict) or set(entry) != {"field_key", "label"}:
            raise ValueError("required field requires field_key and label")
        _text(entry["field_key"], "field key")
        _text(entry["label"], "field label")
    return _json(required_fields, "required fields")


def _candidates(candidates):
    if not isinstance(candidates, list) or len(candidates) > 50:
        raise ValueError("invalid duplicate candidates")
    for entry in candidates:
        if not isinstance(entry, dict) or set(entry) != {"item_id", "title"}:
            raise ValueError("duplicate candidate requires item_id and title")
        _text(entry["item_id"], "candidate item")
        _text(entry["title"], "candidate title")
    return _json(candidates, "duplicate candidates")


def _owned(conn, draft_id, actor):
    _text(draft_id, "create draft")
    _text(actor, "actor")
    row = conn.execute(
        "SELECT * FROM project_bug_create_drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()
    if row is None or row["actor"] != actor:
        raise ValueError("create draft is not owned by this operator")
    return row


def _expect(row, expected_digest):
    _text(expected_digest, "expected digest")
    if row["request_digest"] != expected_digest:
        raise BugConflict("create draft changed; refresh before acting")


def _require_grant(conn, row):
    if not project_create_grants.covers(
        conn,
        grant_id=row["grant_id"],
        actor=row["actor"],
        host=row["host"],
        project_key=row["project_key"],
        type_key=row["type_key"],
    ):
        raise PermissionError("creation is outside current create grant")


def prepare(
    conn,
    *,
    actor,
    request_id,
    grant_id,
    host,
    project_key,
    type_key,
    field_values,
    required_fields,
):
    """Prepare local custody only; native Project creation is a later transport."""
    for label, value in (
        ("actor", actor),
        ("request ID", request_id),
        ("create grant", grant_id),
        ("host", host),
        ("project key", project_key),
        ("type key", type_key),
    ):
        _text(value, label)
    field_values_json = _field_values(field_values)
    required_fields_json = _required_fields(required_fields)
    missing = [entry["field_key"] for entry in required_fields
               if entry["field_key"] not in field_values or empty(field_values[entry["field_key"]])]
    missing_json = canonical_json(missing)
    signature = digest(
        {
            "grant_id": grant_id,
            "host": host,
            "project_key": project_key,
            "type_key": type_key,
            "field_values": field_values,
            "required_fields": required_fields,
        }
    )
    with atomic(conn):
        if request_id.startswith("chat-") and conn.execute(
            "SELECT 1 FROM project_link_intakes WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone():
            raise BugConflict("native message ID already belongs to a Bug import")
        old = conn.execute(
            "SELECT * FROM project_bug_create_drafts WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("create draft request ID reused for different content")
            return dict(old)
        if not project_create_grants.covers(
            conn,
            grant_id=grant_id,
            actor=actor,
            host=host,
            project_key=project_key,
            type_key=type_key,
        ):
            raise PermissionError("creation is outside current create grant")
        draft_id = new_id("pbcd")
        now = iso_now()
        conn.execute(
            """INSERT INTO project_bug_create_drafts
            (draft_id,actor,request_id,request_digest,grant_id,host,project_key,type_key,
             field_values_json,required_fields_json,missing_required_json,
             duplicate_search_id,duplicate_candidates_json,duplicate_confirmed_at,
             state,created_item_id,response_digest,error_code,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,'draft',NULL,NULL,NULL,?,?)""",
            (
                draft_id,
                actor,
                request_id,
                signature,
                grant_id,
                host,
                project_key,
                type_key,
                field_values_json,
                required_fields_json,
                missing_json,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO project_create_grant_events VALUES(?,?,?,?,?)",
            (new_id("pcge"), grant_id, actor, "draft_prepared", iso_now()),
        )
        return dict(
            conn.execute(
                "SELECT * FROM project_bug_create_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
        )


def attach_duplicates(conn, *, draft_id, actor, search_id, candidates):
    _text(search_id, "duplicate search")
    candidates_json = _candidates(candidates)
    with transaction(conn):
        _owned(conn, draft_id, actor)
        result = conn.execute(
            """UPDATE project_bug_create_drafts
            SET duplicate_search_id=?, duplicate_candidates_json=?,
                duplicate_confirmed_at=NULL, state='draft', updated_at=?
            WHERE draft_id=? AND actor=? AND state IN ('draft','ready')""",
            (candidates_json and search_id, candidates_json, iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not editable")
        return dict(_owned(conn, draft_id, actor))


def confirm_not_duplicate(conn, *, draft_id, actor, expected_digest):
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        if row["duplicate_candidates_json"] is None:
            raise ValueError("duplicate search must be attached before confirmation")
        result = conn.execute(
            """UPDATE project_bug_create_drafts
            SET duplicate_confirmed_at=?, updated_at=?
            WHERE draft_id=? AND actor=? AND state IN ('draft','ready')
            AND duplicate_candidates_json IS NOT NULL""",
            (iso_now(), iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not editable")
        return dict(_owned(conn, draft_id, actor))


def mark_ready(conn, *, draft_id, actor, expected_digest):
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        if json.loads(row["missing_required_json"]):
            raise ValueError("required fields are missing")
        if row["duplicate_confirmed_at"] is None:
            raise ValueError("duplicate check must be confirmed")
        _require_grant(conn, row)
        result = conn.execute(
            """UPDATE project_bug_create_drafts SET state='ready', updated_at=?
            WHERE draft_id=? AND actor=? AND state='draft'""",
            (iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not in draft state")
        return dict(_owned(conn, draft_id, actor))


def reopen(conn, *, draft_id, actor, expected_digest):
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        result = conn.execute(
            """UPDATE project_bug_create_drafts SET state='draft', updated_at=?
            WHERE draft_id=? AND actor=? AND state='ready'""",
            (iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not ready")
        return dict(_owned(conn, draft_id, actor))


def cancel(conn, *, draft_id, actor, expected_digest):
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        if row["state"] in IN_FLIGHT_STATES:
            raise BugConflict("in-flight creation cannot be cancelled locally; settle it")
        result = conn.execute(
            """UPDATE project_bug_create_drafts SET state='cancelled', updated_at=?
            WHERE draft_id=? AND actor=? AND state IN ('draft','ready')""",
            (iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft cannot be cancelled")
        return dict(_owned(conn, draft_id, actor))


def reserve_dispatch(conn, *, draft_id, actor, expected_digest, before_reserve=None):
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        _require_grant(conn, row)
        if before_reserve is not None:
            before_reserve(row)
        result = conn.execute(
            """UPDATE project_bug_create_drafts SET state='dispatched', updated_at=?
            WHERE draft_id=? AND actor=? AND state='ready'""",
            (iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not ready")
        return dict(_owned(conn, draft_id, actor))


def settle_created(conn, *, draft_id, actor, created_item_id, response_digest):
    if not isinstance(created_item_id, str) or not ITEM_ID_RE.fullmatch(created_item_id):
        raise ValueError("invalid created item ID")
    _text(response_digest, "response digest")
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        result = conn.execute(
            """UPDATE project_bug_create_drafts
            SET state='created', created_item_id=?, response_digest=?, updated_at=?
            WHERE draft_id=? AND actor=? AND state IN ('dispatched','unknown')""",
            (created_item_id, response_digest, iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not in flight")
        conn.execute(
            "INSERT INTO project_create_grant_events VALUES(?,?,?,?,?)",
            (new_id("pcge"), row["grant_id"], actor, "creation_settled", iso_now()),
        )
        return dict(_owned(conn, draft_id, actor))


def settle_unknown(conn, *, draft_id, actor):
    with transaction(conn):
        _owned(conn, draft_id, actor)
        result = conn.execute(
            """UPDATE project_bug_create_drafts SET state='unknown', updated_at=?
            WHERE draft_id=? AND actor=? AND state='dispatched'""",
            (iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not dispatched")
        return dict(_owned(conn, draft_id, actor))


def settle_rejected(conn, *, draft_id, actor, error_code):
    if error_code not in REJECT_ERROR_CODES:
        raise ValueError("unsupported create rejection error")
    with transaction(conn):
        _owned(conn, draft_id, actor)
        result = conn.execute(
            """UPDATE project_bug_create_drafts
            SET state='rejected', error_code=?, updated_at=?
            WHERE draft_id=? AND actor=? AND state IN ('dispatched','unknown')""",
            (error_code, iso_now(), draft_id, actor),
        )
        if result.rowcount != 1:
            raise BugConflict("create draft is not in flight")
        return dict(_owned(conn, draft_id, actor))


def operator_settle_missing(conn, *, draft_id, actor, expected_digest):
    """Archive an unknown dispatch after the operator verified remote absence."""
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        if row["state"] != "unknown":
            raise BugConflict("only unknown creation results take operator settlement")
    return settle_rejected(
        conn, draft_id=draft_id, actor=actor, error_code="operator_verified_absent"
    )


def operator_settle_found(conn, *, draft_id, actor, expected_digest, created_item_id):
    """Record an unknown dispatch as created after the operator verified the item."""
    with transaction(conn):
        row = _owned(conn, draft_id, actor)
        _expect(row, expected_digest)
        if row["state"] != "unknown":
            raise BugConflict("only unknown creation results take operator settlement")
    return settle_created(
        conn,
        draft_id=draft_id,
        actor=actor,
        created_item_id=created_item_id,
        response_digest=digest({"operator_settled": created_item_id}),
    )


def projection(row):
    """Safe UI view of draft custody without replay or provider digests."""
    candidates = (
        json.loads(row["duplicate_candidates_json"])
        if row["duplicate_candidates_json"] is not None
        else None
    )
    return {
        "draft_id": row["draft_id"],
        "actor": row["actor"],
        "grant_id": row["grant_id"],
        "request_digest": row["request_digest"],
        "scope": {
            "host": row["host"],
            "project_key": row["project_key"],
            "type_key": row["type_key"],
        },
        "field_values": json.loads(row["field_values_json"]),
        "required_fields": json.loads(row["required_fields_json"]),
        "state": row["state"],
        "missing_required": json.loads(row["missing_required_json"]),
        "duplicate_search_id": row["duplicate_search_id"],
        "duplicate_candidates": candidates,
        "duplicate_confirmed": row["duplicate_confirmed_at"] is not None,
        "created_item_id": row["created_item_id"],
        "error_code": row["error_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
