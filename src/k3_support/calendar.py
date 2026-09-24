from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .approvals import (
    ApprovalError,
    expiry_after,
    request_approval,
)
from .config import Config
from .db import transaction
from .ids import canonical_json, digest, new_id
from .lark import CommandResult, run_json
from .operator_notifications import destination as notice_destination
from .operator_notifications import enqueue as enqueue_notice
from .timeutil import iso_now, parse_iso


class CalendarError(ValueError):
    pass


def normalize_meeting_action(
    *,
    case_id: str,
    summary: str,
    start: str,
    end: str,
    attendee_ids: list[str],
    description: str = "",
    room_ids: list[str] | None = None,
    rrule: str | None = None,
    timezone: str = "UTC",
    _historical: bool = False,
) -> dict[str, Any]:
    zone = ZoneInfo(timezone)
    start_dt, end_dt = parse_iso(start), parse_iso(end)
    if start_dt >= end_dt:
        raise CalendarError("meeting end must be after start")
    if not _historical and start_dt <= datetime.now(UTC):
        raise CalendarError("meeting cannot be entirely in the past")
    if not isinstance(summary, str) or not isinstance(description, str):
        raise CalendarError("meeting title and agenda must be text")
    if (
        description.lstrip().startswith("@")
        or description.strip() == "-"
        or "![" in description
    ):
        raise CalendarError(
            "meeting agenda must be inline text without file references or image uploads"
        )
    all_ids = [*attendee_ids, *(room_ids or [])]
    if any(
        not isinstance(value, str)
        or not re.fullmatch(r"(?:ou|oc|omm)_[A-Za-z0-9_]+", value)
        for value in all_ids
    ):
        raise CalendarError("attendee IDs must be resolved stable Feishu IDs")
    if any(not value.startswith("omm_") for value in (room_ids or [])):
        raise CalendarError("room IDs must start with omm_")
    return {
        "attendee_ids": sorted(set(all_ids)),
        "case_id": case_id,
        "description": description,
        "end": end_dt.astimezone(zone).isoformat(),
        "rrule": rrule,
        "start": start_dt.astimezone(zone).isoformat(),
        "summary": summary.strip() or "会议",
        "timezone": timezone,
    }


def create_meeting_preview(
    conn: sqlite3.Connection, *, action: dict[str, Any], valid_minutes: int = 30
) -> dict[str, Any]:
    case_id = action["case_id"]
    action_digest = digest(action)
    now = iso_now()
    with transaction(conn):
        conn.execute(
            """UPDATE approvals SET status='expired',updated_at=?
               WHERE approval_type='meeting_create'
                 AND status IN ('requested','approved') AND expires_at<=?""",
            (now, now),
        )
        conn.execute(
            """UPDATE meeting_previews SET status='expired',updated_at=?
               WHERE status IN ('preview','approved')
                 AND approval_id IN (
                     SELECT approval_id FROM approvals WHERE status='expired'
                 )""",
            (now,),
        )
        existing = conn.execute(
            """SELECT * FROM meeting_previews WHERE case_id=? AND action_digest=?
                 AND status IN ('preview','approved','creating','created')
                 ORDER BY created_at DESC LIMIT 1""",
            (case_id, action_digest),
        ).fetchone()
        effect = {
            key: value
            for key, value in action.items()
            if key not in {"availability", "operation_id"}
        }
        for candidate in conn.execute(
            "SELECT * FROM meeting_previews WHERE case_id=? AND status IN ('preview','approved','creating','created') ORDER BY created_at DESC",
            (case_id,),
        ):
            candidate_action = json.loads(candidate["action_json"])
            if {
                key: value
                for key, value in candidate_action.items()
                if key not in {"availability", "operation_id"}
            } == effect:
                existing = candidate
                break
            if candidate["status"] == "creating":
                raise CalendarError(
                    "calendar create outcome is uncertain; check the original calendar before retrying"
                )
    if existing is not None:
        return dict(existing)
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_create_attempts'"
        ).fetchone()
        and conn.execute(
            "SELECT 1 FROM meeting_create_attempts WHERE case_id=? AND phase NOT IN ('complete','linked','never_dispatched')",
            (case_id,),
        ).fetchone()
    ):
        raise CalendarError("an unresolved calendar attempt blocks a new approval")
    expires_at = expiry_after(valid_minutes)
    if action.get("availability", {}).get("queried_at"):
        evidence_expiry = parse_iso(action["availability"]["queried_at"]) + timedelta(
            minutes=valid_minutes
        )
        expires_at = min(parse_iso(expires_at), evidence_expiry).isoformat()
    approval_id, _, _ = request_approval(
        conn,
        approval_type="meeting_create",
        case_id=case_id,
        action=action,
        expires_at=expires_at,
    )
    preview_id = new_id("mtg")
    now = iso_now()
    with transaction(conn):
        cursor = conn.execute(
            """INSERT OR IGNORE INTO meeting_previews(preview_id,case_id,action_json,action_digest,status,
                   approval_id,created_at,updated_at) VALUES(?,?,?,?, 'preview',?,?,?)""",
            (
                preview_id,
                case_id,
                canonical_json(action),
                action_digest,
                approval_id,
                now,
                now,
            ),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                """SELECT * FROM meeting_previews
                     WHERE case_id=? AND action_digest=?
                       AND status IN ('preview','approved','creating','created')
                     ORDER BY created_at DESC LIMIT 1""",
                (case_id, action_digest),
            ).fetchone()
            if row is None:
                raise CalendarError(
                    "active meeting preview conflict could not be resolved"
                )
            return dict(row)
    return {
        "preview_id": preview_id,
        "approval_id": approval_id,
        "action_digest": action_digest,
        "action": action,
    }


def prepare_conversation_meeting(
    conn: sqlite3.Connection,
    config: Config,
    *,
    case_id: str,
    content: str,
    requester_id: str | None,
    planner: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
    availability_runner: Callable[..., CommandResult] | None = None,
    calendar_runner: Callable[..., CommandResult] | None = None,
) -> dict[str, Any] | None:
    """Turn a clear conversation request into an exact, approval-bound preview."""
    if (
        planner is None
        or not config.feature("calendar")
        or not config.control_operator_id
        or not requester_id
        or not requester_id.startswith("ou_")
    ):
        return None
    raw = planner(
        {
            "message": content,
            "timezone": config.raw["timezone"],
            "now": iso_now(),
            "requester_open_id": requester_id,
        }
    )
    fields = {"summary", "start", "end", "agenda", "include_requester", "confidence"}
    if not isinstance(raw, dict) or set(raw) != fields:
        return None
    if (
        not isinstance(raw["summary"], str)
        or not raw["summary"].strip()
        or not isinstance(raw["start"], str)
        or not isinstance(raw["end"], str)
        or not isinstance(raw["agenda"], str)
        or not isinstance(raw["include_requester"], bool)
        or raw["include_requester"] is not True
        or not isinstance(raw["confidence"], (int, float))
        or isinstance(raw["confidence"], bool)
        or float(raw["confidence"]) < 0.92
    ):
        return None
    try:
        action = normalize_meeting_action(
            case_id=case_id,
            summary=raw["summary"],
            start=raw["start"],
            end=raw["end"],
            attendee_ids=[requester_id],
            description=raw["agenda"],
            timezone=config.raw["timezone"],
        )
    except (CalendarError, ValueError):
        return None
    from .calendar_availability import query_availability

    action["availability"] = query_availability(
        config, action, runner=availability_runner
    )
    from .meeting_recovery import bind_meeting_action

    # A missing read adapter is not permission to approve a mutable "primary"
    # target or the old shortcut's implicit rollback and notification defaults.
    if calendar_runner is None:
        return None
    action = bind_meeting_action(config, action, runner=calendar_runner)
    return queue_meeting_preview(conn, config, action=action)


def queue_meeting_preview(conn, config, *, action):
    """Queue an exact review card; queuing a preview does not create a meeting."""
    preview = create_meeting_preview(conn, action=action)
    if preview.get("status") in {"creating", "created"}:
        return preview
    return _queue_preview_message(conn, config, preview=preview, action=action)


def queue_reserved_meeting_preview(conn, config, *, preview_id, expected_digest):
    """Recover only the reserved preview's notice, never synthesize a replacement."""
    preview, action = historical_meeting_preview(conn, preview_id)
    if preview['action_digest'] != expected_digest:
        raise CalendarError('reserved meeting preview digest changed')
    return _queue_preview_message(conn, config, preview=dict(preview), action=action, require_pending=True)


def _queue_preview_message(conn, config, *, preview, action, require_pending=False):
    if preview.get("action_json"):
        action = json.loads(preview["action_json"])
    approval_id = str(preview["approval_id"])
    case_id = action["case_id"]
    text = (
        "📅 会议创建预览\n"
        f"Case: {case_id}\n"
        f"主题: {action['summary'][:200]}{'…（完整内容见预览）' if len(action['summary']) > 200 else ''}\n"
        f"时间: {action['start']} → {action['end']}\n"
        f"时区: {action['timezone']}\n"
        f"参会对象: {len(action['attendee_ids'])}（完整 ID 见预览）\n"
        "请先查看完整议程、参会人和忙闲边界，再确认创建。当前没有创建或邀请。"
    )

    with transaction(conn):
        if require_pending:
            exact, _ = exact_meeting_preview(conn, preview['preview_id'])
            if (exact['approval_status'] != 'requested' or exact['consumed_at'] is not None
                    or parse_iso(exact['expires_at']) <= datetime.now(UTC)
                    or exact['action_digest'] != preview['action_digest']):
                raise CalendarError('reserved meeting approval expired or changed; no replacement was created')
        outbox_id = None
        if notice_destination(config):
            outbox_id, _ = enqueue_notice(
                conn, config,
                action_type="approval_request",
                payload={
                    "text": text,
                    "approval_id": approval_id,
                    "approval_type": "meeting_create",
                    "preview_id": preview["preview_id"],
                    "meeting_action_digest": preview["action_digest"],
                    "buttons": [
                        {
                            "text": "查看完整预览",
                            "callback_data": f"mt:v:{preview['preview_id'][4:]}:1.{preview['action_digest'][:16]}",
                        },
                        {"text": "❌ 不创建", "callback_data": f"k3a:d:{approval_id}"},
                    ],
                },
                idempotency_key=f"meeting-preview:{preview['preview_id']}:approval",
                case_id=case_id,
            )
    return {**preview, "outbox_id": outbox_id}


def execute_meeting_create(
    conn: sqlite3.Connection,
    config: Config,
    *,
    preview_id: str,
    runner: Callable[..., CommandResult] = run_json,
) -> dict[str, Any]:
    from .meeting_recovery import MeetingRecoveryError, execute_bound_meeting

    try:
        return execute_bound_meeting(conn, config, preview_id=preview_id, runner=runner)
    except MeetingRecoveryError as exc:
        raise CalendarError(str(exc)) from exc


def historical_meeting_preview(conn, preview_id):
    """Validate immutable historical content, without granting current authority."""
    preview = conn.execute(
        """SELECT mp.*,a.status approval_status,a.expires_at,a.consumed_at,
           a.requested_action_json,a.action_digest AS approved_digest,
           a.lifecycle_round AS approval_round,c.lifecycle_round AS current_round,c.state AS case_state
           FROM meeting_previews mp JOIN approvals a ON a.approval_id=mp.approval_id
           JOIN cases c ON c.case_id=mp.case_id WHERE mp.preview_id=?""",
        (preview_id,),
    ).fetchone()
    if preview is None:
        raise CalendarError("meeting preview not found")
    action = json.loads(preview["action_json"])
    if (
        digest(action) != preview["action_digest"]
        or preview["action_digest"] != preview["approved_digest"]
        or canonical_json(action)
        != canonical_json(json.loads(preview["requested_action_json"]))
        or action.get("case_id") != preview["case_id"]
    ):
        raise ApprovalError("meeting exact action changed; generate a new preview")
    normalized = normalize_meeting_action(
        case_id=action["case_id"],
        summary=action["summary"],
        start=action["start"],
        end=action["end"],
        attendee_ids=action["attendee_ids"],
        description=action["description"],
        rrule=action.get("rrule"),
        timezone=action["timezone"],
        _historical=True,
    )
    extra = {"availability"}
    if action.get("schema_version") == 2:
        from .meeting_recovery import validate_bound_action

        validate_bound_action(action)
        extra |= {
            "schema_version",
            "operation_id",
            "target",
            "transport_profile",
            "create_body",
            "attendees_body",
            "failure_policy",
            "mail_source",
        }
    if {key: action.get(key) for key in normalized} != normalized or set(action) - set(
        normalized
    ) - extra:
        raise ApprovalError("meeting exact action changed; generate a new preview")
    return preview, action


def exact_meeting_preview(conn, preview_id):
    """Historical integrity plus current lifecycle/time authorization checks."""
    preview, action = historical_meeting_preview(conn, preview_id)
    from .mail_meeting_prepare import validate_source

    validate_source(conn, action)
    if preview["approval_round"] != preview["current_round"] or preview[
        "case_state"
    ] in {"resolved", "cancelled", "paused", "takeover"}:
        raise ApprovalError("meeting preview is stale for the current Case authority")
    if parse_iso(action["start"]) <= datetime.now(UTC):
        raise ApprovalError("meeting time is past; generate a new exact preview")
    return preview, action
