"""Operator-authored local drafts, not calendar actions or approvals."""

import json
from uuid import UUID
from zoneinfo import ZoneInfo

from .calendar import normalize_meeting_action
from .db import transaction
from .ids import canonical_json
from .mail_actions import _digest, view
from .timeutil import iso_now


def read(conn, message_id):
    source = view(conn, message_id)
    preparation = conn.execute(
        "SELECT p.request_id,p.binding_json,p.state,p.draft_revision,p.preview_id,m.approval_id,"
        "c.request_id AS cancellation_id FROM mail_meeting_prepare p "
        "LEFT JOIN mail_meeting_prepare_cancellations c ON c.prepare_request_id=p.request_id "
        "LEFT JOIN meeting_previews m ON m.preview_id=p.preview_id WHERE p.message_id=? "
        "ORDER BY p.draft_revision DESC LIMIT 1", (message_id,)
    ).fetchone()
    if preparation:
        preparation = dict(preparation)
        preparation["binding_digest"] = _digest(json.loads(preparation.pop("binding_json")))
        if preparation.pop("cancellation_id"):
            preparation["state"] = "cancelled"
    row = conn.execute(
        "SELECT * FROM mail_meeting_drafts WHERE message_id=?", (message_id,)
    ).fetchone()
    return {
        "preparation": dict(preparation) if preparation else None,
        "message_id": message_id,
        "source_digest": source["content_digest"],
        "revision": row["revision"] if row else 0,
        "source_changed": bool(
            row and row["source_digest"] != source["content_digest"]
        ),
        "draft": json.loads(row["draft_json"])
        if row
        else {
            "summary": source["header"]["subject"] or "会议",
            "description": "",
            "start": "",
            "end": "",
            "timezone": "Asia/Shanghai",
            "attendee_ids": [],
        },
        "calendar_created": False,
        "requires_exact_preview_and_approval": True,
    }


def save(
    conn, *, message_id, draft, expected_revision, source_digest, request_id, actor_id
):
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("operator required")
    if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("canonical UUID required")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("invalid revision")
    fields = {"summary", "description", "start", "end", "timezone", "attendee_ids"}
    if not isinstance(draft, dict) or set(draft) != fields:
        raise ValueError("invalid draft fields")
    if any(
        not isinstance(draft[key], str) or len(draft[key]) > 8000
        for key in fields - {"attendee_ids"}
    ):
        raise ValueError("draft fields must be bounded text")
    ids = draft["attendee_ids"]
    if (
        not isinstance(ids, list)
        or len(ids) > 100
        or any(not isinstance(v, str) for v in ids)
    ):
        raise ValueError("invalid attendee list")
    # Partial drafts may omit BOTH times. Stable IDs still undergo the existing validator.
    ZoneInfo(draft["timezone"])
    if bool(draft["start"]) != bool(draft["end"]):
        raise ValueError("provide both start and end, or leave both empty")
    normalize_meeting_action(
        case_id="draft-only",
        summary=draft["summary"],
        description=draft["description"],
        start=draft["start"] or "2099-01-01T09:00:00+00:00",
        end=draft["end"] or "2099-01-01T10:00:00+00:00",
        attendee_ids=ids,
        timezone=draft["timezone"],
        _historical=True,
    )
    fingerprint = _digest(
        {
            "message_id": message_id,
            "draft": draft,
            "expected_revision": expected_revision,
            "source_digest": source_digest,
            "actor_id": actor_id,
        }
    )
    with transaction(conn):
        old = conn.execute(
            "SELECT * FROM mail_meeting_draft_history WHERE request_id=?", (request_id,)
        ).fetchone()
        if old:
            if old["request_digest"] != fingerprint:
                raise ValueError("request ID reused with different draft")
            return {**json.loads(old["result_json"]), "replayed": True}
        current = read(conn, message_id)
        if (
            current["revision"] != expected_revision
            or current["source_digest"] != source_digest
        ):
            raise ValueError("draft or mail changed; refresh")
        conn.execute(
            "INSERT INTO mail_meeting_drafts VALUES(?,?,?,?,?,?) ON CONFLICT(message_id) "
            "DO UPDATE SET revision=excluded.revision,source_digest=excluded.source_digest,"
            "draft_json=excluded.draft_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (
                message_id,
                expected_revision + 1,
                source_digest,
                canonical_json(draft),
                actor_id,
                iso_now(),
            ),
        )
        result = read(conn, message_id)
        conn.execute(
            "INSERT INTO mail_meeting_draft_history VALUES(?,?,?,?,?)",
            (
                request_id,
                fingerprint,
                message_id,
                expected_revision + 1,
                canonical_json(result),
            ),
        )
        return {**result, "replayed": False}
