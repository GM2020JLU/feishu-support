"""Read-only calendar preparation in the sync worker; creation remains approved."""

import json
from uuid import UUID

from .approvals import ApprovalError
from .calendar import create_meeting_preview, normalize_meeting_action
from .calendar_availability import query_availability
from .db import transaction
from .ids import canonical_json
from .mail_actions import _digest, view
from .mail_meeting_drafts import read
from .meeting_recovery import bind_meeting_action
from .runtime_control import capability_allowed
from .timeutil import iso_now


def binding(conn, message_id):
    draft = read(conn, message_id)
    mail = view(conn, message_id)
    if draft["revision"] == 0 or draft["source_changed"]:
        raise ValueError("save a current draft first")
    case_id = mail["linked_case_id"]
    case = conn.execute(
        "SELECT state,lifecycle_round FROM cases WHERE case_id=?", (case_id,)
    ).fetchone()
    if case is None or case["state"] in {"resolved", "cancelled", "paused", "takeover"}:
        raise ValueError("link an active Case before preparing")
    if not draft["draft"]["start"] or not draft["draft"]["attendee_ids"]:
        raise ValueError("complete times and explicit attendees first")
    normalize_meeting_action(case_id=case_id, **draft["draft"])
    return {
        "message_id": message_id,
        "draft_revision": draft["revision"],
        "source_digest": draft["source_digest"],
        "mail_revision": mail["revision"],
        "case_id": case_id,
        "approval_round": case["lifecycle_round"],
        "draft": draft["draft"],
    }


def validate_source(conn, action):
    if "mail_source" not in action:
        return
    source = action["mail_source"]
    try:
        valid = binding(conn, source["message_id"]) == source
        revoked = conn.execute(
            "SELECT 1 FROM mail_meeting_prepare p JOIN mail_meeting_prepare_cancellations c "
            "ON c.prepare_request_id=p.request_id WHERE p.message_id=? AND p.draft_revision=?",
            (source["message_id"], source["draft_revision"]),
        ).fetchone()
        valid = valid and not revoked
        if valid and "summary" in action:
            expected = normalize_meeting_action(
                case_id=source["case_id"], **source["draft"]
            )
            valid = all(action.get(key) == value for key, value in expected.items())
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ApprovalError("mail or meeting draft changed; prepare a new preview")


def enqueue(
    conn, *, message_id, expected_revision, source_digest, request_id, actor_id
):
    if (
        not isinstance(actor_id, str)
        or not actor_id.strip()
        or not isinstance(request_id, str)
        or str(UUID(request_id)) != request_id
    ):
        raise ValueError("authenticated actor and canonical UUID required")
    with transaction(conn):
        value = binding(conn, message_id)
        if (
            type(expected_revision) is not int
            or value["draft_revision"] != expected_revision
            or value["source_digest"] != source_digest
        ):
            raise ValueError("draft changed; refresh")
        old = conn.execute(
            "SELECT * FROM mail_meeting_prepare WHERE request_id=? OR (message_id=? AND draft_revision=?)",
            (request_id, message_id, expected_revision),
        ).fetchone()
        if old:
            if (
                old["binding_json"] != canonical_json(value)
                or old["actor_id"] != actor_id
            ):
                raise ValueError("preparation request conflicts with current draft")
            if conn.execute(
                "SELECT 1 FROM mail_meeting_prepare_cancellations WHERE prepare_request_id=?",
                (old["request_id"],),
            ).fetchone():
                raise ValueError(
                    "preparation cancelled; save a new draft version first"
                )
            return dict(old)
        now = iso_now()
        conn.execute(
            "INSERT INTO mail_meeting_prepare VALUES(?,?,?,?,'queued',NULL,?,?,?)",
            (
                request_id,
                message_id,
                expected_revision,
                canonical_json(value),
                actor_id,
                now,
                now,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM mail_meeting_prepare WHERE request_id=?", (request_id,)
            ).fetchone()
        )


def dispatch_one(conn, config, *, runner):
    if (
        config.mode != "active"
        or not config.feature("calendar")
        or not capability_allowed(conn, config, "calendar")
    ):
        return None
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM mail_meeting_prepare p WHERE state='queued' AND NOT EXISTS "
            "(SELECT 1 FROM mail_meeting_prepare_cancellations c WHERE c.prepare_request_id=p.request_id) "
            "ORDER BY created_at,request_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE mail_meeting_prepare SET state='dispatched',updated_at=? WHERE request_id=?",
            (iso_now(), row["request_id"]),
        )
    # A crash leaves dispatched, never automatically reclaimed or retried.
    preview_id = None
    state = "needs_review"
    try:
        source = json.loads(row["binding_json"])
        validate_source(conn, {"mail_source": source})
        action = normalize_meeting_action(case_id=source["case_id"], **source["draft"])
        action["availability"] = query_availability(config, action, runner=runner)
        action = bind_meeting_action(config, action, runner=runner)
        action["mail_source"] = source
        validate_source(conn, action)
        preview = create_meeting_preview(conn, action=action)
        preview_id = preview["preview_id"]
        # No Telegram notification queued here; the GUI shows the exact approval.
        validate_source(conn, action)
        state = "prepared"
    except Exception:  # noqa: BLE001 - no secret-bearing adapter errors or automatic retry
        state = "needs_review"
    with transaction(conn):
        conn.execute(
            "UPDATE mail_meeting_prepare SET state=?,preview_id=?,updated_at=? WHERE request_id=? AND state='dispatched'",
            (state, preview_id, iso_now(), row["request_id"]),
        )
    return {"request_id": row["request_id"], "state": state, "preview_id": preview_id}


def cancel(conn, *, prepare_request_id, binding_digest, request_id, actor_id):
    """Fence all late previews. This never cancels or deletes a remote meeting."""
    if (
        not isinstance(actor_id, str)
        or not actor_id.strip()
        or not isinstance(request_id, str)
        or str(UUID(request_id)) != request_id
    ):
        raise ValueError("authenticated actor and canonical UUID required")
    with transaction(conn):
        prior = conn.execute(
            "SELECT * FROM mail_meeting_prepare_cancellations WHERE request_id=? OR prepare_request_id=?",
            (request_id, prepare_request_id),
        ).fetchone()
        if prior:
            if (
                prior["prepare_request_id"],
                prior["binding_digest"],
                prior["actor_id"],
            ) != (prepare_request_id, binding_digest, actor_id):
                raise ValueError("cancellation request conflicts")
            return {
                "cancelled": True,
                "replayed": True,
                "remote_calendar_changed": False,
            }
        row = conn.execute(
            "SELECT * FROM mail_meeting_prepare WHERE request_id=?",
            (prepare_request_id,),
        ).fetchone()
        if row is None or _digest(json.loads(row["binding_json"])) != binding_digest:
            raise ValueError("preparation changed; refresh")
        # Include crash-orphaned previews not yet recorded in the queue row.
        running = conn.execute(
            "SELECT 1 FROM meeting_previews m JOIN approvals a ON a.approval_id=m.approval_id "
            "WHERE json_extract(m.action_json,'$.mail_source.message_id')=? "
            "AND json_extract(m.action_json,'$.mail_source.draft_revision')=? "
            "AND (a.consumed_at IS NOT NULL OR m.status IN ('creating','created'))",
            (row["message_id"], row["draft_revision"]),
        ).fetchone()
        if running:
            raise ValueError(
                "meeting execution already began; use original calendar recovery"
            )
        conn.execute(
            "INSERT INTO mail_meeting_prepare_cancellations VALUES(?,?,?,?,?)",
            (prepare_request_id, request_id, binding_digest, actor_id, iso_now()),
        )
        return {"cancelled": True, "replayed": False, "remote_calendar_changed": False}
