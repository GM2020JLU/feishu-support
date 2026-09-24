"""Durable one-shot Project writes with scoped authority and read-only recovery.

No automatic replay of dispatched operations. No network inside SQLite writes.
Dispatch is the linearization point: revocation before it prevents the write;
after it the operation is in flight and must be reconciled, not declared undone.
"""

import json
import time

from .db import atomic, transaction
from .ids import canonical_json, digest, new_id
from .project_bug_grants import covers
from .project_bugs import BugConflict, _bug, _event, _revision, _text
from .project_transport import ProjectReceipt, ProjectView
from .runtime_control import current_global_state
from .timeutil import iso_now, parse_iso, utc_now

TERMINAL = {"confirmed", "rejected", "partial", "cancelled", "conflict", "satisfied"}

# Actions whose authorization the official mutation endpoint enforces itself. A
# transport advertising anything outside this set is distrusted entirely.
_SERVER_ENFORCEABLE = frozenset(
    {"bug.comment", "bug.fields", "bug.transition", "bug.close"}
)

# Lifecycle status and its operator attribution are controlled only through
# validated workflow transitions. A generic fields write must not bypass the
# transition/close approval path, even if a grant includes an overly broad key.
_RESERVED_BUG_FIELDS = frozenset(
    {"work_item_status", "current_status_operator", "current_status_operator_role"}
)


def _json(value):
    result = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(result.encode()) > 256_000:
        raise ValueError("Bug write exceeds size limit")
    return result


def _change(action, change):
    if not isinstance(change, dict):
        raise ValueError("invalid Bug change")  # noqa: TRY004 -- schema validation contract
    if action == "bug.fields":
        if (
            set(change) not in ({"fields"}, {"fields", "required_target_status_id", "required_missing_fields"})
            or not isinstance(change["fields"], dict)
            or not change["fields"]
        ):
            raise ValueError("field write requires a nonempty field map")
        if "required_missing_fields" in change:
            keys = change["required_missing_fields"]
            if (not isinstance(keys, list) or not keys
                    or not all(isinstance(key, str) and key in change["fields"] for key in keys)
                    or len(keys) != len(set(keys))):
                raise ValueError("invalid required-missing field keys")
            _text(change["required_target_status_id"], "required target state")
        for key in change["fields"]:
            _text(key, "field key")
        if _RESERVED_BUG_FIELDS.intersection(change["fields"]):
            raise ValueError("reserved workflow field cannot be written through bug.fields")
    elif action == "bug.comment":
        if set(change) != {"text"}:
            raise ValueError("comment requires text only")
        _text(change["text"], "comment", 20000)
    elif action in {"bug.transition", "bug.close"}:
        if set(change) != {"transition_id", "target_status_id"}:
            raise ValueError(
                "transition requires exact transition and target state IDs"
            )
        for value in change.values():
            _text(value, "transition identity")
    else:
        raise ValueError("unsupported Bug write")
    _json(change)


def _operation(conn, operation_id):
    row = conn.execute(
        "SELECT * FROM project_bug_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Bug operation does not exist")
    return row


def _snapshot(conn, bug_id, snapshot_id):
    row = conn.execute(
        "SELECT payload_json FROM project_bug_snapshots WHERE bug_id=? AND snapshot_id=?",
        (bug_id, snapshot_id),
    ).fetchone()
    if row is None:
        raise ValueError("snapshot does not belong to this Bug")
    snapshot = json.loads(row[0])
    source = conn.execute(
        "SELECT evidence_json FROM project_bug_read_evidence WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    snapshot["fields"] = observed_fields(snapshot, json.loads(source[0]) if source else {})
    return snapshot


def observed_fields(snapshot, evidence=None):
    """Share missing-versus-null semantics across preparation, preview and I/O."""
    if evidence is None:
        evidence = snapshot.get("read_evidence", {})
    missing = set(evidence.get("unobserved_field_keys", [])) | set(
        evidence.get("omitted_value_field_keys", [])
    )
    return {key: value for key, value in snapshot["fields"].items() if key not in missing}


def _destination(bug):
    return {key: bug[key] for key in ("host", "project_key", "type_key", "item_id")}


def _target(bug, action, change):
    return {key: bug[key] for key in ("host", "project_key", "type_key", "bug_id")} | {
        "action": action,
        "fields": sorted(change.get("fields", {})),
        "transition": change.get("transition_id"),
        "repository": None,
        "device": None,
    }


def field_diff(base, current, proposed):
    """Missing is unavailable, not JSON null. Never blindly write whole records."""
    result = []
    for key, wanted in sorted(proposed.items()):
        known = key in base and key in current
        if not known:
            state = "unavailable"
        elif canonical_json(current[key]) == canonical_json(wanted):
            state = "already_matches"
        elif canonical_json(current[key]) != canonical_json(base[key]):
            state = "conflict"
        else:
            state = "change"
        result.append(
            {
                "field": key,
                "base_present": key in base,
                "current_present": key in current,
                "base": base.get(key),
                "current": current.get(key),
                "proposed": wanted,
                "state": state,
            }
        )
    return result


def prepare(
    conn,
    *,
    bug_id,
    snapshot_id,
    actor,
    request_id,
    grant_id,
    expected_revision,
    action,
    change,
):
    _text(actor, "actor")
    _text(request_id, "request ID")
    _change(action, change)
    signature = digest(
        {
            "bug_id": bug_id,
            "snapshot_id": snapshot_id,
            "grant_id": grant_id,
            "revision": expected_revision,
            "action": action,
            "change": change,
        }
    )
    with atomic(conn):
        old = conn.execute(
            "SELECT * FROM project_bug_operations WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if old:
            if old["request_digest"] != signature:
                raise BugConflict("write request ID reused for different content")
            return dict(old)
        bug = _bug(conn, bug_id)
        _revision(bug, expected_revision)
        base = _snapshot(conn, bug_id, snapshot_id)
        latest = conn.execute(
            "SELECT snapshot_id FROM project_bug_snapshots WHERE bug_id=? ORDER BY sequence DESC LIMIT 1",
            (bug_id,),
        ).fetchone()
        if latest is None or latest[0] != snapshot_id:
            raise BugConflict("write requires the current snapshot")
        required_missing = set(change.get("required_missing_fields", []))
        if action == "bug.fields" and (
            any(key not in base["fields"] and key not in required_missing
                for key in change["fields"])
            or any(key in base["fields"] for key in required_missing)
        ):
            raise ValueError("cannot write fields absent from the source snapshot")
        if not covers(
            conn, grant_id=grant_id, actor=actor, target=_target(bug, action, change)
        ):
            raise PermissionError("write is outside current grant")
        if conn.execute(
            "SELECT 1 FROM project_bug_operations WHERE bug_id=? AND state IN ('prepared','dispatched','unknown')",
            (bug_id,),
        ).fetchone():
            raise BugConflict(
                "Bug has an unsettled write; reconcile or cancel it first"
            )
        case = conn.execute(
            "SELECT version FROM cases WHERE case_id=?", (bug["case_id"],)
        ).fetchone()
        operation_id = new_id("pbo")
        conn.execute(
            """INSERT INTO project_bug_operations
            (operation_id,bug_id,snapshot_id,grant_id,actor,request_id,request_digest,
             bug_revision,case_version,action,change_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                bug_id,
                snapshot_id,
                grant_id,
                actor,
                request_id,
                signature,
                bug["revision"],
                case["version"],
                action,
                canonical_json(change),
                iso_now(),
                iso_now(),
            ),
        )
        _event(conn, bug_id, actor, "write_prepared", {"operation_id": operation_id})
        return dict(_operation(conn, operation_id))


def _guard(conn, config, op):
    from .project_result_comment import guard

    guard(conn, op)
    settings = config.raw.get("project_integration", {})
    state = current_global_state(conn, config)
    if (
        settings.get("write_enabled") is not True
        or config.mode != "active"
        or state["mode"] not in {"collaborate", "auto", "auto_60"}
    ):
        raise PermissionError("Project writes are disabled by runtime policy")
    if op["actor"] != config.control_operator_id:
        raise PermissionError("operation is not owned by configured operator")
    bug = _bug(conn, op["bug_id"])
    _revision(bug, op["bug_revision"])
    case = conn.execute(
        "SELECT state,version FROM cases WHERE case_id=?", (bug["case_id"],)
    ).fetchone()
    if case["version"] != op["case_version"] or case["state"] in {
        "paused",
        "takeover",
        "cancelled",
        "resolved",
        "error",
        "escalated",
    }:
        raise BugConflict("Case changed or is not delegated for writes")
    active_round = conn.execute(
        "SELECT execution_state FROM project_bug_rounds WHERE bug_id=? AND archived_at IS NULL",
        (bug["bug_id"],),
    ).fetchone()
    if active_round and active_round[0] in {"paused", "human", "unknown"}:
        raise BugConflict("investigation is paused, held or unsettled")
    change = json.loads(op["change_json"])
    if not covers(
        conn,
        grant_id=op["grant_id"],
        actor=op["actor"],
        target=_target(bug, op["action"], change),
    ):
        raise PermissionError("grant expired, revoked or does not cover write")
    return bug, digest({"runtime": state, "configuration": config.raw})


def preview(conn, operation_id, current):
    op = _operation(conn, operation_id)
    base = _snapshot(conn, op["bug_id"], op["snapshot_id"])
    change = json.loads(op["change_json"])
    differences = (
        field_diff(base["fields"], observed_fields(current), change["fields"])
        if op["action"] == "bug.fields"
        else []
    )
    required_missing = set(change.get("required_missing_fields", []))
    for item in differences:
        if item["field"] in required_missing:
            item["state"] = (
                "missing_required"
                if not item["base_present"] and not item["current_present"]
                else "conflict"
            )
    return {
        "operation_id": operation_id,
        "action": op["action"],
        "state": op["state"],
        "change": change,
        "differences": differences,
        "status_changed": base["status_id"] != current["status_id"],
        "schema_changed": base["schema_digest"] != current["schema_digest"],
    }


def _packet(conn, op, bug, view, *, config=None):
    if type(view) is not ProjectView or view.destination != _destination(bug):
        raise ValueError("adapter preflight destination mismatch")
    age = (utc_now() - parse_iso(view.observed_at)).total_seconds()
    if not 0 <= age <= 60:
        raise BugConflict("adapter preflight is stale")
    action = op["action"]
    server_enforced = (
        view.server_enforced_actions
        if view.server_enforced_actions <= _SERVER_ENFORCEABLE
        else frozenset()
    )
    if action not in view.allowed_actions and action not in server_enforced:
        raise PermissionError("current Project permission does not permit write")
    shown = preview(conn, op["operation_id"], view.snapshot)
    if shown["schema_changed"]:
        raise BugConflict("Project schema changed; refresh and prepare again")
    change = shown["change"]
    if action == "bug.fields" and all(
        item["state"] == "already_matches" for item in shown["differences"]
    ):
        # Desired state is observed, but do not attribute it to this operation.
        return None
    if action in {"bug.fields", "bug.transition", "bug.close"} and (
        action not in view.conditional_actions or not view.conditional_token
    ):
        raise PermissionError("adapter has no verified conditional-write contract")
    if action == "bug.fields":
        required_missing = set(change.get("required_missing_fields", []))
        if (not (set(change["fields"]) - required_missing <= view.writable_fields)
                or not required_missing <= view.fillable_required_fields):
            raise PermissionError("field is not an ordinary writable field")
        if any(
            item["state"] in {"unavailable", "conflict"}
            for item in shown["differences"]
        ):
            raise BugConflict(
                "colleague edit or unavailable field; resolve the difference"
            )
    closure_approval_id = None
    if action in {"bug.transition", "bug.close"}:
        transition = view.transitions.get(change["transition_id"])
        if (
            shown["status_changed"]
            or not isinstance(transition, dict)
            or set(transition) != {"target_status_id", "closes", "required_complete"}
            or transition["target_status_id"] != change["target_status_id"]
            or type(transition["closes"]) is not bool
            or transition["required_complete"] is not True
        ):
            raise BugConflict(
                "transition changed, unavailable or missing required data"
            )
        if transition["closes"] != (action == "bug.close"):
            # A closing transition must be requested as bug.close and vice versa;
            # closure is never inferred from a job or downgraded to an ordinary
            # transition.
            raise BugConflict(
                "closure classification must match the requested action"
            )
        if action == "bug.close":
            from .project_close_gate import consume as consume_close_approval

            closure_approval_id = consume_close_approval(
                conn, bug=bug, change=change, operation_id=op["operation_id"], config=config
            )
    packet = {
        "operation_id": op["operation_id"],
        "destination": _destination(bug),
        "action": action,
        "change": change,
        "conditional_token": view.conditional_token,
        "source_snapshot_id": op["snapshot_id"],
        "permission_basis": "preflight_verified" if action in view.allowed_actions else "server_checked_at_write",
    }
    if closure_approval_id is not None:
        packet["closure_approval_id"] = closure_approval_id
    return packet


def dispatch(
    conn,
    config,
    *,
    operation_id,
    transport,
    before_dispatch=None,
    before_settle=None,
    unknown_recheck=(),
    sleep=time.sleep,
):
    """One remote attempt; preflight is read-only, all outcomes use reconciliation."""
    op = _operation(conn, operation_id)
    if op["state"] != "prepared":
        raise BugConflict(
            "operation was dispatched or settled; never dispatch it again"
        )
    with transaction(conn):
        if before_dispatch is not None:
            before_dispatch()
        bug, fence = _guard(conn, config, op)
    try:
        view = transport.preflight(_destination(bug))
    except Exception:  # noqa: BLE001 -- sanitize provider failures at the adapter boundary
        raise RuntimeError(
            "Project preflight failed; no write was dispatched"
        ) from None
    with transaction(conn):
        if before_dispatch is not None:
            before_dispatch()
        op = _operation(conn, operation_id)
        if op["state"] != "prepared":
            raise BugConflict("operation changed while checking Project")
        bug, current_fence = _guard(conn, config, op)
        if fence != current_fence:
            raise BugConflict("runtime policy changed during preflight")
        packet = _packet(conn, op, bug, view, config=config)
        if packet is None:
            result = {
                "outcome": "already_matches",
                "write_performed": False,
                "observed_at": view.observed_at,
            }
            conn.execute(
                "UPDATE project_bug_operations SET state='satisfied',result_json=?,updated_at=? WHERE operation_id=?",
                (canonical_json(result), iso_now(), operation_id),
            )
            _event(
                conn,
                op["bug_id"],
                "project_adapter",
                "write_not_needed",
                {"operation_id": operation_id},
            )
            return dict(_operation(conn, operation_id))
        conn.execute(
            "UPDATE project_bug_operations SET state='dispatched',write_json=?,write_digest=?,updated_at=? WHERE operation_id=?",
            (canonical_json(packet), digest(packet), iso_now(), operation_id),
        )
        _event(
            conn,
            op["bug_id"],
            op["actor"],
            "write_dispatched",
            {"operation_id": operation_id},
        )
    try:
        transport.write(packet)
    except Exception:  # noqa: BLE001 -- all transport failures leave effects uncertain
        # Never persist arbitrary provider errors (which can contain credentials).
        # An exception cannot establish whether the server accepted the mutation.
        _unknown(conn, operation_id)
    result = reconcile(
        conn, operation_id=operation_id, transport=transport, before_settle=before_settle
    )
    # The remote acknowledged writes become visible in snapshot/history reads a
    # few seconds later (observed 2026-09-18: reconcile immediately after a
    # confirmed field update read stale state). A bounded re-check keeps the
    # common case automatic; anything still unknown stays for human settlement.
    for delay in unknown_recheck:
        if result["state"] != "unknown":
            break
        sleep(delay)
        result = reconcile(
            conn,
            operation_id=operation_id,
            transport=transport,
            before_settle=before_settle,
        )
    return result


def _unknown(conn, operation_id):
    with transaction(conn):
        conn.execute(
            "UPDATE project_bug_operations SET state='unknown',updated_at=? WHERE operation_id=? AND state='dispatched'",
            (iso_now(), operation_id),
        )


def _receipt(op, receipt):
    if (
        type(receipt) is not ProjectReceipt
        or receipt.operation_id != op["operation_id"]
        or receipt.write_digest != op["write_digest"]
        or type(receipt.terminal) is not bool
        or receipt.outcome not in {"applied", "rejected", "partial", "unknown"}
    ):
        raise ValueError("invalid or mismatched Project receipt")
    if receipt.outcome == "unknown" or not receipt.terminal:
        return "unknown", {"outcome": "unknown"}
    _text(receipt.evidence_ref, "remote receipt reference", 2000)
    fields = json.loads(op["change_json"]).get("fields", {})
    if (
        not isinstance(receipt.applied_fields, tuple)
        or any(not isinstance(key, str) for key in receipt.applied_fields)
        or len(set(receipt.applied_fields)) != len(receipt.applied_fields)
        or not set(receipt.applied_fields) <= set(fields)
    ):
        raise ValueError("receipt has unexpected applied fields")
    if receipt.outcome == "partial" and (
        not fields or not 0 < len(receipt.applied_fields) < len(fields)
    ):
        raise ValueError("partial result requires an exact applied subset")
    if (
        receipt.outcome == "applied"
        and fields
        and set(receipt.applied_fields) != set(fields)
    ):
        raise ValueError("applied result must cover the complete field set")
    if receipt.outcome == "rejected" and receipt.applied_fields:
        raise ValueError("rejected result cannot contain applied fields")
    state = {"applied": "confirmed", "rejected": "rejected", "partial": "partial"}[
        receipt.outcome
    ]
    return state, {
        "outcome": receipt.outcome,
        "evidence_ref": receipt.evidence_ref,
        "applied_fields": list(receipt.applied_fields),
        "terminal": True,
    }


def reconcile(conn, *, operation_id, transport, before_settle=None):
    """Works after revocation/pause: observing old effects never reauthorizes them."""
    op = _operation(conn, operation_id)
    if op["state"] in TERMINAL:
        return dict(op)
    if op["state"] not in {"dispatched", "unknown"}:
        raise BugConflict("operation has not been dispatched")
    try:
        receipt = transport.reconcile(json.loads(op["write_json"]))
        state, result = _receipt(op, receipt)
    except Exception:  # noqa: BLE001 -- failed observations cannot clear uncertainty
        state, result = "unknown", {"outcome": "unknown"}
    with transaction(conn):
        if before_settle is not None:
            before_settle()
        latest = _operation(conn, operation_id)
        if latest["state"] in TERMINAL:
            return dict(latest)
        conn.execute(
            "INSERT INTO project_bug_operation_observations VALUES(?,?,?,?)",
            (new_id("pboo"), operation_id, canonical_json(result), iso_now()),
        )
        conn.execute(
            "UPDATE project_bug_operations SET state=?,result_json=?,updated_at=? WHERE operation_id=?",
            (state, canonical_json(result), iso_now(), operation_id),
        )
        _event(
            conn,
            op["bug_id"],
            "project_adapter",
            "write_observed",
            {"operation_id": operation_id, "state": state},
        )
        return dict(_operation(conn, operation_id))


def cancel(conn, *, operation_id, actor, expected_digest):
    with transaction(conn):
        op = _operation(conn, operation_id)
        if actor != op["actor"] or expected_digest != op["request_digest"]:
            raise PermissionError("operation owner or content mismatch")
        if op["state"] == "cancelled":
            return dict(op)
        if op["state"] != "prepared":
            raise BugConflict(
                "in-flight effects cannot be cancelled locally; reconcile"
            )
        conn.execute(
            "UPDATE project_bug_operations SET state='cancelled',updated_at=? WHERE operation_id=?",
            (iso_now(), operation_id),
        )
        _event(
            conn, op["bug_id"], actor, "write_cancelled", {"operation_id": operation_id}
        )
        return dict(_operation(conn, operation_id))
