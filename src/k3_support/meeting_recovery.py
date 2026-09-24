"""Fenced calendar creation and bounded, externally read-only recovery.

No timeout, empty search, 404, expired lease or owner assertion proves absence.
Only an atomically fenced *pre-dispatch* attempt can authorize a fresh preview.
Remote receipts are append-only facts, independent of current Case authority.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .approvals import ApprovalError, expiry_after, verify_control_identity
from .db import transaction
from .ids import canonical_json, digest, new_id
from .lark import CommandResult
from .timeutil import iso_now, parse_iso

PROFILE = "calendar-v4-exact-v1"
TERMINAL = {"complete", "linked", "never_dispatched"}


class MeetingRecoveryError(ApprovalError):
    pass


def _data(result):
    if (
        not isinstance(result, CommandResult)
        or result.identity != "user"
        or not isinstance(result.data, dict)
    ):
        raise MeetingRecoveryError("calendar response has no trusted user envelope")
    return result.data


def _calendar_id(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.@+-]{3,300}", value)
        or value == "primary"
    ):
        raise MeetingRecoveryError("calendar must be a fixed, concrete ID")
    return value


def resolve_calendar_target(config, *, runner):
    """Read the current user's primary calendar; never resolve a historic target."""
    data = _data(
        runner(
            [
                "calendar",
                "calendars",
                "primary",
                "--user-id-type",
                "open_id",
                "--as",
                "user",
            ]
        )
    )
    rows = data.get("calendars")
    owner = config.raw.get("identity", {}).get("feishu_owner_open_id")
    if not owner or not isinstance(rows, list) or len(rows) != 1:
        raise MeetingRecoveryError(
            "original calendar and owner could not be uniquely verified"
        )
    row = rows[0]
    cal = row.get("calendar") if isinstance(row, dict) else None
    if (
        not isinstance(cal, dict)
        or row.get("user_id") != owner
        or cal.get("type") != "primary"
        or cal.get("role") not in {"owner", "writer"}
        or cal.get("is_deleted") is not False
        or cal.get("is_third_party") is not False
    ):
        raise MeetingRecoveryError(
            "current calendar identity or write authority does not match the owner"
        )
    return {
        "calendar_id": _calendar_id(cal.get("calendar_id")),
        "actor_open_id": owner,
        "identity": "user",
        "user_id_type": "open_id",
    }


def _bodies(action):
    body = {
        "summary": action["summary"],
        "description": action["description"],
        "start_time": {
            "timestamp": str(int(parse_iso(action["start"]).timestamp())),
            "timezone": action["timezone"],
        },
        "end_time": {
            "timestamp": str(int(parse_iso(action["end"]).timestamp())),
            "timezone": action["timezone"],
        },
        "vchat": {"vc_type": "vc"},
        "visibility": "default",
        "attendee_ability": "can_modify_event",
        "free_busy_status": "busy",
        "reminders": [{"minutes": 5}],
        "need_notification": True,
        "recurrence": action.get("rrule") or "",
    }
    attendees = []
    for identifier in action["attendee_ids"]:
        kind, field = (
            ("user", "user_id")
            if identifier.startswith("ou_")
            else ("chat", "chat_id")
            if identifier.startswith("oc_")
            else ("resource", "room_id")
        )
        attendees.append({"type": kind, field: identifier})
    return body, {
        "attendees": attendees,
        "need_notification": True,
        "is_enable_admin": False,
        "add_operator_to_attendee": False,
    }


def bind_meeting_action(config, action, *, runner):
    """Prepare v2 approval content from the same exact bodies the adapter sends."""
    from .calendar import normalize_meeting_action

    base = normalize_meeting_action(
        **{
            key: action[key]
            for key in (
                "case_id",
                "summary",
                "start",
                "end",
                "attendee_ids",
                "description",
                "rrule",
                "timezone",
            )
        }
    )
    body, attendees = _bodies(base)
    if "availability" in action:
        base["availability"] = action["availability"]
    return {
        **base,
        "schema_version": 2,
        "operation_id": new_id("mop")[4:],
        "target": resolve_calendar_target(config, runner=runner),
        "transport_profile": PROFILE,
        "create_body": body,
        "attendees_body": attendees,
        "failure_policy": "keep_created_event_no_automatic_retry_or_delete",
    }


def validate_bound_action(action):
    if (
        action.get("schema_version") != 2
        or action.get("transport_profile") != PROFILE
        or not re.fullmatch(r"[0-9a-f]{32}", str(action.get("operation_id", "")))
    ):
        raise MeetingRecoveryError(
            "legacy or unsupported meeting action requires a new exact preview"
        )
    target = action.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"calendar_id", "actor_open_id", "identity", "user_id_type"}
        or target["identity"] != "user"
        or target["user_id_type"] != "open_id"
        or not re.fullmatch(r"ou_[A-Za-z0-9_]+", str(target["actor_open_id"]))
    ):
        raise MeetingRecoveryError("meeting has no fixed user/calendar target")
    _calendar_id(target["calendar_id"])
    body, attendees = _bodies(action)
    if (
        action.get("create_body") != body
        or action.get("attendees_body") != attendees
        or action.get("failure_policy")
        != "keep_created_event_no_automatic_retry_or_delete"
    ):
        raise MeetingRecoveryError("meeting bodies or side effects changed")


def revise_bound_action(action):
    """Rebuild explicitly displayed bodies for a newly reviewed time option."""
    body, attendees = _bodies(action)
    result = {
        **action,
        "operation_id": new_id("mop")[4:],
        "create_body": body,
        "attendees_body": attendees,
    }
    validate_bound_action(result)
    return result


def _append(conn, attempt_id, kind, payload):
    encoded, checksum = canonical_json(payload), digest(payload)
    observation_id = new_id("mro")
    conn.execute(
        "INSERT OR IGNORE INTO meeting_recovery_observations(observation_id,attempt_id,kind,payload_json,payload_digest,created_at) VALUES(?,?,?,?,?,?)",
        (observation_id, attempt_id, kind, encoded, checksum, iso_now()),
    )
    row = conn.execute(
        "SELECT observation_id FROM meeting_recovery_observations WHERE attempt_id=? AND kind=? AND payload_digest=?",
        (attempt_id, kind, checksum),
    ).fetchone()
    return row[0]


def _attempt(conn, attempt_id):
    row = conn.execute(
        "SELECT * FROM meeting_create_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise MeetingRecoveryError("meeting attempt not found")
    return dict(row)


def _check_attempt_integrity(attempt, action):
    if attempt["action_digest"] != digest(action) or attempt[
        "action_json"
    ] != canonical_json(action):
        raise MeetingRecoveryError(
            "meeting attempt differs from its immutable approval action"
        )
    if action.get("schema_version") == 2 and attempt["target_json"] != canonical_json(
        action["target"]
    ):
        raise MeetingRecoveryError(
            "meeting attempt target differs from its approved target"
        )


def _authority(conn, config, attempt):
    from .calendar import exact_meeting_preview
    from .runtime_control import capability_allowed, current_global_state

    preview, action = exact_meeting_preview(conn, attempt["preview_id"])
    if (
        not config.feature("calendar")
        or config.mode != "active"
        or not capability_allowed(conn, config, "calendar")
    ):
        raise MeetingRecoveryError("calendar capability is not active")
    if (
        preview["approval_status"] != "consumed"
        or not preview["consumed_at"]
        or parse_iso(preview["expires_at"]) <= datetime.now(UTC)
    ):
        raise MeetingRecoveryError(
            "meeting approval no longer authorizes this operation"
        )
    if action.get("target", {}).get("actor_open_id") != config.raw.get(
        "identity", {}
    ).get("feishu_owner_open_id") or action != json.loads(attempt["action_json"]):
        raise MeetingRecoveryError("meeting target or exact action changed")
    state = current_global_state(conn, config)
    if attempt["global_fence"] != state["outbound_fence"]:
        raise MeetingRecoveryError("meeting global authority changed")
    return action


def begin_attempt(conn, config, preview_id):
    from .calendar import exact_meeting_preview
    from .runtime_control import capability_allowed, current_global_state

    with transaction(conn):
        preview, action = exact_meeting_preview(conn, preview_id)
        validate_bound_action(action)
        if (
            not config.feature("calendar")
            or config.mode != "active"
            or not capability_allowed(conn, config, "calendar")
        ):
            raise MeetingRecoveryError("calendar creation is disabled")
        if action["target"]["actor_open_id"] != config.raw.get("identity", {}).get(
            "feishu_owner_open_id"
        ):
            raise MeetingRecoveryError("meeting owner changed")
        if (
            preview["approval_status"] != "approved"
            or preview["consumed_at"] is not None
            or parse_iso(preview["expires_at"]) <= datetime.now(UTC)
        ):
            raise MeetingRecoveryError("meeting requires unconsumed exact approval")
        if conn.execute(
            "SELECT 1 FROM meeting_create_attempts WHERE case_id=? AND phase NOT IN ('complete','linked','never_dispatched')",
            (preview["case_id"],),
        ).fetchone():
            raise MeetingRecoveryError("an unresolved calendar attempt blocks creation")
        now, attempt_id = iso_now(), new_id("mat")
        conn.execute(
            "UPDATE approvals SET status='consumed',consumed_at=?,updated_at=? WHERE approval_id=?",
            (now, now, preview["approval_id"]),
        )
        conn.execute(
            "INSERT INTO meeting_create_attempts(attempt_id,preview_id,case_id,lifecycle_round,action_digest,action_json,target_json,phase,dispatch_token,global_fence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'prepared',?,?,?,?)",
            (
                attempt_id,
                preview_id,
                preview["case_id"],
                preview["current_round"],
                preview["action_digest"],
                canonical_json(action),
                canonical_json(action["target"]),
                new_id("mdt"),
                current_global_state(conn, config)["outbound_fence"],
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE meeting_previews SET status='creating',remote_result_json=?,updated_at=? WHERE preview_id=?",
            (
                canonical_json({"outcome": "prepared", "attempt_id": attempt_id}),
                now,
                preview_id,
            ),
        )
    return _attempt(conn, attempt_id)


def enter_dispatch(conn, config, *, attempt_id, dispatch_token, invitation=False):
    """The only transition granting one external call; a spent marker cannot retry."""
    with transaction(conn):
        attempt = _attempt(conn, attempt_id)
        expected = "event_created" if invitation else "prepared"
        if (
            attempt["dispatch_token"] != dispatch_token
            or attempt["phase"] != expected
            or attempt["successor_preview_id"]
        ):
            raise MeetingRecoveryError("meeting dispatch is spent, stale or superseded")
        action = _authority(conn, config, attempt)
        phase = "inviting" if invitation else "dispatched"
        conn.execute(
            "UPDATE meeting_create_attempts SET phase=?,invitation_dispatched_at=CASE WHEN ? THEN ? ELSE invitation_dispatched_at END,revision=revision+1,updated_at=? WHERE attempt_id=? AND phase=? AND revision=?",
            (
                phase,
                invitation,
                iso_now(),
                iso_now(),
                attempt_id,
                expected,
                attempt["revision"],
            ),
        )
    return action


def record_event_receipt(conn, *, attempt_id, dispatch_token, result):
    """Persist a real adapter receipt before any mutable Case/projection checks."""
    if conn.in_transaction:
        raise MeetingRecoveryError(
            "event receipt needs an independent durable transaction"
        )
    data = _data(result)
    event = data.get("event")
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("event_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", event["event_id"])
    ):
        raise MeetingRecoveryError("calendar create returned no event receipt")
    with transaction(conn):
        attempt = _attempt(conn, attempt_id)
        if attempt["dispatch_token"] != dispatch_token or attempt["phase"] in {
            "prepared",
            "never_dispatched",
            "legacy_uncertain",
        }:
            raise MeetingRecoveryError("receipt has no matching entered dispatch")
        target = json.loads(attempt["target_json"])
        payload = {"identity": "user", "event": event}
        if conn.execute(
            "SELECT 1 FROM meeting_recovery_observations WHERE attempt_id=? AND kind='event_receipt' AND payload_digest=?",
            (attempt_id, digest(payload)),
        ).fetchone():
            if attempt["phase"] == "conflict":
                raise MeetingRecoveryError(
                    "conflicting calendar receipt is already retained"
                )
            return event["event_id"]
        # A receipt for an unexpected calendar is retained as a conflict, never
        # discarded or used to authorize invitations to that calendar.
        conflict = (
            attempt["phase"] == "conflict"
            or event.get("organizer_calendar_id") != target["calendar_id"]
            or (
                attempt["event_id"] is not None
                and attempt["event_id"] != event["event_id"]
            )
        )
        _append(conn, attempt_id, "event_receipt", payload)
        phase = (
            "conflict"
            if conflict
            else "partial"
            if attempt["phase"] == "adopted"
            else attempt["phase"]
            if attempt["phase"] in {"inviting", "complete", "partial", "linked"}
            else "event_created"
        )
        conn.execute(
            "UPDATE meeting_create_attempts SET phase=?,event_id=coalesce(event_id,?),revision=revision+1,updated_at=? WHERE attempt_id=?",
            (phase, event["event_id"], iso_now(), attempt_id),
        )
        conn.execute(
            "UPDATE meeting_previews SET status=?,calendar_event_id=coalesce(calendar_event_id,?),remote_result_json=?,updated_at=? WHERE preview_id=?",
            (
                "created" if phase in {"complete", "linked"} else "creating",
                event["event_id"],
                canonical_json(
                    {
                        "outcome": phase,
                        "attempt_id": attempt_id,
                        "event_id": event["event_id"],
                    }
                ),
                iso_now(),
                attempt["preview_id"],
            ),
        )
    if conflict:
        raise MeetingRecoveryError(
            "conflicting calendar receipt retained for owner review"
        )
    return event["event_id"]


def record_failure(conn, *, attempt_id, error):
    with transaction(conn):
        attempt = _attempt(conn, attempt_id)
        _append(
            conn,
            attempt_id,
            "error",
            {"phase": attempt["phase"], "error_class": type(error).__name__},
        )
        # Pre-dispatch failure remains prepared: only an explicit atomic cancel
        # may turn it into a reliable never_dispatched proof.
        phase = (
            "uncertain"
            if attempt["phase"] == "dispatched"
            else "partial"
            if attempt["phase"] in {"event_created", "inviting"}
            else attempt["phase"]
        )
        conn.execute(
            "UPDATE meeting_create_attempts SET phase=?,revision=revision+1,updated_at=? WHERE attempt_id=?",
            (phase, iso_now(), attempt_id),
        )
        conn.execute(
            "UPDATE meeting_previews SET remote_result_json=?,updated_at=? WHERE preview_id=?",
            (
                canonical_json(
                    {
                        "outcome": phase,
                        "attempt_id": attempt_id,
                        "event_id": attempt["event_id"],
                        "next_action": "核对原结果；禁止自动重试创建或邀请",
                    }
                ),
                iso_now(),
                attempt["preview_id"],
            ),
        )


def _attendee_keys(items):
    keys, rooms, observed_statuses = set(), {}, {}
    if not isinstance(items, list):
        raise MeetingRecoveryError("attendee list is not complete structured evidence")
    for item in items:
        if not isinstance(item, dict):
            raise MeetingRecoveryError("invalid attendee record")
        kind = item.get("type")
        field = {"user": "user_id", "chat": "chat_id", "resource": "room_id"}.get(kind)
        if field is None or not isinstance(item.get(field), str):
            raise MeetingRecoveryError("attendee identity is unavailable")
        key = (kind, item[field])
        status = item.get("rsvp_status", "unknown")
        if not isinstance(status, str):
            raise MeetingRecoveryError("attendee RSVP status is invalid")
        if key in observed_statuses and observed_statuses[key] != status:
            raise MeetingRecoveryError("contradictory attendee RSVP records")
        observed_statuses[key] = status
        if status == "removed":
            continue
        keys.add(key)
        if kind == "resource":
            rooms[item[field]] = status
    return keys, rooms


def _rooms_booked(expected, room_statuses):
    """Report only the requested rooms; no room request is not a booking."""
    requested = {identity for kind, identity in expected if kind == "resource"}
    if not requested:
        return None
    return all(room_statuses.get(identity) == "accept" for identity in requested)


def finish_invitation(conn, *, attempt_id, dispatch_token, result):
    if conn.in_transaction:
        raise MeetingRecoveryError(
            "invitation receipt needs an independent durable transaction"
        )
    data = _data(result)
    items = data.get("attendees")
    with transaction(conn):
        attempt = _attempt(conn, attempt_id)
        if (
            attempt["dispatch_token"] != dispatch_token
            or not attempt["invitation_dispatched_at"]
        ):
            raise MeetingRecoveryError("invitation receipt has no matching dispatch")
        action = json.loads(attempt["action_json"])
        expected, _ = _attendee_keys(action["attendees_body"]["attendees"])
        if conn.execute(
            "SELECT 1 FROM meeting_recovery_observations WHERE attempt_id=? AND kind='attendee_receipt' AND payload_digest=?",
            (attempt_id, digest({"identity": "user", "attendees": items})),
        ).fetchone():
            stored = conn.execute(
                "SELECT remote_result_json FROM meeting_previews WHERE preview_id=?",
                (attempt["preview_id"],),
            ).fetchone()
            return {**json.loads(stored[0]), "replayed": True}
        _append(
            conn,
            attempt_id,
            "attendee_receipt",
            {"identity": "user", "attendees": items},
        )
        valid_membership = True
        try:
            actual, rooms = _attendee_keys(items)
        except MeetingRecoveryError:
            actual, rooms = set(), {}
            valid_membership = False
        permitted = expected | {("user", action["target"]["actor_open_id"])}
        complete = valid_membership and expected <= actual and actual <= permitted
        phase = (
            attempt["phase"]
            if attempt["phase"] in {"conflict", "linked"}
            else "complete"
            if complete
            else "partial"
        )
        conn.execute(
            "UPDATE meeting_create_attempts SET phase=?,revision=revision+1,updated_at=? WHERE attempt_id=?",
            (phase, iso_now(), attempt_id),
        )
        outcome = {
            "event_id": attempt["event_id"],
            "attempt_id": attempt_id,
            "outcome": phase,
            "invitation_membership_confirmed": complete,
            "room_statuses": rooms,
            "rooms_booked": _rooms_booked(expected, rooms),
        }
        conn.execute(
            "UPDATE meeting_previews SET status=?,calendar_event_id=?,remote_result_json=?,updated_at=? WHERE preview_id=?",
            (
                "created" if phase in {"complete", "linked"} else "creating",
                attempt["event_id"],
                canonical_json(outcome),
                iso_now(),
                attempt["preview_id"],
            ),
        )
    return outcome


def execute_bound_meeting(conn, config, *, preview_id, runner):
    attempt = begin_attempt(conn, config, preview_id)
    action = json.loads(attempt["action_json"])
    target = action["target"]
    try:
        if resolve_calendar_target(config, runner=runner) != target:
            raise MeetingRecoveryError("calendar identity changed since approval")
        enter_dispatch(
            conn,
            config,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
        )
        result = runner(
            [
                "calendar",
                "events",
                "create",
                "--calendar-id",
                target["calendar_id"],
                "--user-id-type",
                "open_id",
                "--idempotency-key",
                action["operation_id"],
                "--data",
                canonical_json(action["create_body"]),
                "--as",
                "user",
            ]
        )
        event_id = record_event_receipt(
            conn,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
            result=result,
        )
        if not action["attendees_body"]["attendees"]:
            # Even the no-invite finalization rechecks current authority. The
            # already-created event is retained when authority has changed.
            enter_dispatch(
                conn,
                config,
                attempt_id=attempt["attempt_id"],
                dispatch_token=attempt["dispatch_token"],
                invitation=True,
            )
            return finish_invitation(
                conn,
                attempt_id=attempt["attempt_id"],
                dispatch_token=attempt["dispatch_token"],
                result=CommandResult({"attendees": []}, "user", []),
            )
        if resolve_calendar_target(config, runner=runner) != target:
            raise MeetingRecoveryError("calendar identity changed before invitation")
        enter_dispatch(
            conn,
            config,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
            invitation=True,
        )
        invitation = runner(
            [
                "calendar",
                "event.attendees",
                "create",
                "--calendar-id",
                target["calendar_id"],
                "--event-id",
                event_id,
                "--user-id-type",
                "open_id",
                "--data",
                canonical_json(action["attendees_body"]),
                "--as",
                "user",
            ]
        )
        return finish_invitation(
            conn,
            attempt_id=attempt["attempt_id"],
            dispatch_token=attempt["dispatch_token"],
            result=invitation,
        )
    except Exception as exc:
        record_failure(conn, attempt_id=attempt["attempt_id"], error=exc)
        raise MeetingRecoveryError(
            "calendar attempt requires recovery; do not replay creation or invitations"
        ) from exc


@dataclass(frozen=True)
class RecoveryBudget:
    max_pages: int = 5
    max_events: int = 1000
    max_candidates: int = 5
    max_attendee_pages: int = 10
    deadline_seconds: float = 20.0

    def __post_init__(self):
        if (
            any(
                type(value) is not int or not 1 <= value <= limit
                for value, limit in (
                    (self.max_pages, 20),
                    (self.max_events, 10000),
                    (self.max_candidates, 20),
                    (self.max_attendee_pages, 40),
                )
            )
            or not 0 < self.deadline_seconds <= 120
        ):
            raise MeetingRecoveryError("invalid bounded calendar recovery budget")


class CalendarReader:
    """Only these read endpoints are reachable; no caller-supplied path/method."""

    def __init__(self, runner, *, monotonic=time.monotonic):
        self.runner, self.monotonic = runner, monotonic
        self.deadline = None

    def _run(self, argv):
        remaining = 20.0 if self.deadline is None else self.deadline - self.monotonic()
        if remaining <= 0:
            raise MeetingRecoveryError("calendar recovery deadline exhausted")
        return self.runner(argv, timeout=min(remaining, 30.0))

    def target(self, config):
        return resolve_calendar_target(config, runner=self._run)

    def events(self, target, *, anchor, token=None):
        params = {"anchor_time": anchor, "page_size": 200, "user_id_type": "open_id"}
        if token:
            params["page_token"] = token
        return _data(
            self._run(
                [
                    "api",
                    "GET",
                    f"/open-apis/calendar/v4/calendars/{quote(_calendar_id(target['calendar_id']), safe='')}/events",
                    "--params",
                    canonical_json(params),
                    "--as",
                    "user",
                ]
            )
        )

    def event(self, target, event_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,300}", str(event_id)):
            raise MeetingRecoveryError("invalid event ID")
        data = _data(
            self._run(
                [
                    "calendar",
                    "events",
                    "get",
                    "--calendar-id",
                    _calendar_id(target["calendar_id"]),
                    "--event-id",
                    event_id,
                    "--need-meeting-settings",
                    "--user-id-type",
                    "open_id",
                    "--as",
                    "user",
                ]
            )
        )
        event = data.get("event")
        if not isinstance(event, dict) or event.get("event_id") != event_id:
            raise MeetingRecoveryError("event response does not match requested ID")
        return event

    def attendees(self, target, event_id, *, token=None):
        argv = [
            "calendar",
            "event.attendees",
            "list",
            "--calendar-id",
            _calendar_id(target["calendar_id"]),
            "--event-id",
            event_id,
            "--page-size",
            "100",
            "--user-id-type",
            "open_id",
            "--as",
            "user",
        ]
        if token:
            argv.extend(["--page-token", token])
        return _data(self._run(argv))


def _page(data):
    if (
        not isinstance(data.get("items"), list)
        or type(data.get("has_more")) is not bool
    ):
        raise MeetingRecoveryError("calendar pagination protocol is incomplete")
    token = data.get("page_token")
    if data["has_more"] and (
        not isinstance(token, str) or not token or len(token) > 8192
    ):
        raise MeetingRecoveryError("calendar pagination cursor is missing")
    return data["items"], token if data["has_more"] else None


def _event_match(action, event):
    body = action.get("create_body") or _bodies(action)[0]
    fields = (
        "summary",
        "description",
        "start_time",
        "end_time",
        "visibility",
        "attendee_ability",
        "free_busy_status",
        "reminders",
        "recurrence",
    )
    if action.get("schema_version") != 2:
        fields = ("summary", "description", "start_time", "end_time", "recurrence")
    mismatches = [field for field in fields if event.get(field) != body[field]]
    if (
        action.get("schema_version") == 2
        and (event.get("vchat") or {}).get("vc_type") != body["vchat"]["vc_type"]
    ):
        mismatches.append("vchat")
    if event.get("status") != "confirmed" or event.get("is_exception") is not False:
        mismatches.append("status_or_recurrence_instance")
    return mismatches


def _scan(reader, config, attempt, action, budget, target_hint=None):
    started = reader.monotonic()
    reader.deadline = started + budget.deadline_seconds
    target = (
        json.loads(attempt["target_json"]) if attempt["target_json"] else target_hint
    )
    result = {
        "classification": "inconclusive",
        "reason": "original_target_unknown",
        "target": target,
        "candidates": [],
        "pagination_complete": False,
        "events_observed": 0,
        "checked_at": iso_now(),
        "attempt_revision": attempt["revision"],
        "action_digest": attempt["action_digest"],
        "target_basis": "original_dispatch_target"
        if attempt["target_json"]
        else "operator_search_scope"
        if target_hint
        else "unknown",
        "origin_receipt_verified": False,
    }
    if not target:
        return result

    def deadline():
        if reader.monotonic() - started >= budget.deadline_seconds:
            raise MeetingRecoveryError("calendar recovery deadline exhausted")

    try:
        deadline()
        current_target = reader.target(config)
        if any(
            current_target.get(field) != target.get(field)
            for field in ("actor_open_id", "identity", "user_id_type")
        ):
            raise MeetingRecoveryError("original user/calendar identity changed")
        anchor = str(int(parse_iso(action["start"]).timestamp()))
        result["anchor_time"] = anchor
        ids, tokens, token = set(), set(), None
        # A known receipt is a direct target, but it does not eliminate the need
        # to read all requested participant identities.
        known_receipt = attempt["event_id"] and attempt.get("has_event_receipt")
        result["origin_receipt_verified"] = bool(known_receipt)
        if attempt["event_id"]:
            ids.add(attempt["event_id"])
            result["pagination_complete"] = True
        else:
            for _ in range(budget.max_pages):
                deadline()
                items, following = _page(
                    reader.events(target, anchor=anchor, token=token)
                )
                result["events_observed"] += len(items)
                if result["events_observed"] > budget.max_events:
                    raise MeetingRecoveryError(
                        "calendar recovery event budget exhausted"
                    )
                for event in items:
                    if not isinstance(event, dict):
                        raise MeetingRecoveryError("invalid calendar event page")
                    if event.get("summary") == action["summary"]:
                        if not isinstance(event.get("event_id"), str):
                            raise MeetingRecoveryError("candidate event has no ID")
                        ids.add(event["event_id"])
                if len(ids) > budget.max_candidates:
                    raise MeetingRecoveryError(
                        "calendar recovery candidate budget exhausted"
                    )
                if following is None:
                    result["pagination_complete"] = True
                    break
                if following in tokens:
                    raise MeetingRecoveryError("calendar pagination made no progress")
                tokens.add(following)
                token = following
            if not result["pagination_complete"]:
                raise MeetingRecoveryError("calendar recovery page budget exhausted")
        for event_id in sorted(ids):
            deadline()
            event = reader.event(target, event_id)
            mismatch = _event_match(action, event)
            if event.get("organizer_calendar_id") != target["calendar_id"]:
                mismatch.append("organizer_calendar_id")
            expected, _ = _attendee_keys(
                action.get("attendees_body", _bodies(action)[1])["attendees"]
            )
            attendees, tokens, token = [], set(), None
            complete = False
            for _ in range(budget.max_attendee_pages):
                deadline()
                items, following = _page(
                    reader.attendees(target, event_id, token=token)
                )
                attendees.extend(items)
                if following is None:
                    complete = True
                    break
                if following in tokens:
                    raise MeetingRecoveryError("attendee pagination made no progress")
                tokens.add(following)
                token = following
            if not complete:
                raise MeetingRecoveryError("attendee recovery page budget exhausted")
            actual, rooms = _attendee_keys(attendees)
            # The owner/organizer is allowed in addition to the explicit invite
            # set; arbitrary extra guests are not silently treated as exact.
            permitted = expected | {("user", target["actor_open_id"])}
            if not expected <= actual or actual - permitted:
                mismatch.append("attendee_membership")
            candidate = {
                "event_id": event_id,
                "exact": not mismatch,
                "mismatches": mismatch,
                "event_digest": digest(event),
                "attendee_digest": digest(attendees),
                "room_statuses": rooms,
                "rooms_booked": _rooms_booked(expected, rooms),
                "historical_fields_missing": []
                if action.get("schema_version") == 2
                else [
                    "original_target",
                    "visibility",
                    "notification_policy",
                    "attendee_ability",
                    "reminders",
                    "video_settings",
                    "failure_policy",
                ],
            }
            result["candidates"].append(candidate)
        exact = [item for item in result["candidates"] if item["exact"]]
        if len(exact) == 1 and len(result["candidates"]) == 1:
            result.update(
                classification="known_created"
                if known_receipt
                else "legacy_candidate"
                if action.get("schema_version") != 2
                else "unique_candidate",
                reason="complete_exact_match",
            )
        elif len(exact) > 1:
            result.update(
                classification="multiple_candidates", reason="multiple_exact_events"
            )
        elif result["candidates"]:
            result.update(
                classification="partial_match", reason="event_or_invitation_not_exact"
            )
        else:
            result["reason"] = "no_match_does_not_prove_absence"
    except Exception as exc:  # noqa: BLE001 - external read/protocol failure must never become absence
        result.update(
            classification="inconclusive",
            reason=str(exc)
            if isinstance(exc, MeetingRecoveryError)
            else "calendar_read_failed",
            error_class=type(exc).__name__,
        )
    return result


def check_meeting_creation(
    conn,
    config,
    *,
    preview_id,
    reader,
    budgets=None,
    target_hint=None,
    control_message=None,
):
    from .calendar import historical_meeting_preview

    _preview, action = historical_meeting_preview(conn, preview_id)
    row = conn.execute(
        "SELECT * FROM meeting_create_attempts WHERE preview_id=?", (preview_id,)
    ).fetchone()
    if (
        row is None
        and action.get("schema_version") != 2
        and _preview["status"] == "creating"
    ):
        # A pre-upgrade process can enter creating after migration 032. Register
        # only its uncertainty; do not invent a dispatched marker or target.
        with transaction(conn):
            current, current_action = historical_meeting_preview(conn, preview_id)
            if current["status"] == "creating" and current_action == action:
                conn.execute(
                    "INSERT OR IGNORE INTO meeting_create_attempts(attempt_id,preview_id,case_id,lifecycle_round,action_digest,action_json,phase,dispatch_token,created_at,updated_at) VALUES(?,?,?,?,?,?,'legacy_uncertain',?,?,?)",
                    (
                        "mat_legacy_" + preview_id,
                        preview_id,
                        current["case_id"],
                        current["approval_round"],
                        current["action_digest"],
                        canonical_json(action),
                        "legacy_" + preview_id,
                        iso_now(),
                        iso_now(),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM meeting_create_attempts WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
    if row is None:
        raise MeetingRecoveryError("meeting has no attempted creation to recover")
    attempt = dict(row)
    _check_attempt_integrity(attempt, action)
    if target_hint is not None:
        if control_message is None:
            raise MeetingRecoveryError(
                "legacy search scope requires an explicit owner request"
            )
        verify_control_identity(
            config, control_message.user_id, control_message.chat_id
        )
        if attempt["target_json"] is not None:
            raise MeetingRecoveryError(
                "an original calendar target cannot be replaced by a search hint"
            )
        if (
            not isinstance(target_hint, dict)
            or set(target_hint)
            != {"calendar_id", "actor_open_id", "identity", "user_id_type"}
            or target_hint
            != {
                "calendar_id": _calendar_id(target_hint.get("calendar_id")),
                "actor_open_id": config.raw.get("identity", {}).get(
                    "feishu_owner_open_id"
                ),
                "identity": "user",
                "user_id_type": "open_id",
            }
        ):
            raise MeetingRecoveryError("invalid explicit calendar search scope")
    attempt["has_event_receipt"] = bool(
        conn.execute(
            "SELECT 1 FROM meeting_recovery_observations WHERE attempt_id=? AND kind='event_receipt'",
            (attempt["attempt_id"],),
        ).fetchone()
    )
    observation = _scan(
        reader, config, attempt, action, budgets or RecoveryBudget(), target_hint
    )
    with transaction(conn):
        current = _attempt(conn, attempt["attempt_id"])
        if current["revision"] != attempt["revision"]:
            observation.update(
                classification="inconclusive", reason="attempt_changed_during_check"
            )
        observation_id = _append(conn, attempt["attempt_id"], "check", observation)
    return {
        **observation,
        "observation_id": observation_id,
        "observation_digest": digest(observation),
        "external_writes": False,
    }


def meeting_recovery_report(conn, config, *, preview_id, observation_limit=50):
    from .calendar import historical_meeting_preview

    _preview, action = historical_meeting_preview(conn, preview_id)
    if type(observation_limit) is not int or not 1 <= observation_limit <= 200:
        raise MeetingRecoveryError("invalid observation limit")
    row = conn.execute(
        "SELECT * FROM meeting_create_attempts WHERE preview_id=?", (preview_id,)
    ).fetchone()
    if row is None:
        uncertain_legacy = _preview["status"] == "creating" or (
            _preview["consumed_at"] is not None and _preview["status"] != "created"
        )
        return {
            "preview_id": preview_id,
            "phase": "legacy_uncertain"
            if uncertain_legacy
            else "legacy_created"
            if _preview["status"] == "created"
            else "not_attempted",
            "read_only": True,
            "action": action,
            "action_digest": digest(action),
            "requires_new_v2_preview": action.get("schema_version") != 2
            and not uncertain_legacy
            and _preview["status"] != "created",
            "original_outcome_uncertain": uncertain_legacy,
            "event_id": _preview["calendar_event_id"],
        }
    attempt = dict(row)
    _check_attempt_integrity(attempt, action)
    observations = [
        {
            "observation_id": item["observation_id"],
            "kind": item["kind"],
            "payload": json.loads(item["payload_json"]),
            "digest": item["payload_digest"],
            "created_at": item["created_at"],
        }
        for item in conn.execute(
            "SELECT * FROM meeting_recovery_observations WHERE attempt_id=? ORDER BY rowid DESC LIMIT ?",
            (attempt["attempt_id"], observation_limit),
        )
    ]
    return {
        "preview_id": preview_id,
        "attempt_id": attempt["attempt_id"],
        "phase": attempt["phase"],
        "revision": attempt["revision"],
        "event_id": attempt["event_id"],
        "action": action,
        "action_digest": attempt["action_digest"],
        "observations": observations,
        "observation_total": conn.execute(
            "SELECT count(*) FROM meeting_recovery_observations WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0],
        "read_only": True,
        "external_writes": False,
        "can_cancel_before_dispatch": attempt["phase"] == "prepared",
        "can_repreview": attempt["phase"] == "never_dispatched"
        and not attempt["successor_preview_id"],
        "can_auto_retry": False,
        "successor_preview_id": attempt["successor_preview_id"],
        "original_outcome_uncertain": attempt["phase"]
        in {"legacy_uncertain", "uncertain", "adopted", "dispatched"},
        "adopted_target": json.loads(attempt["adopted_target_json"])
        if attempt["adopted_target_json"]
        else None,
    }


def bind_existing_meeting(
    conn,
    config,
    *,
    observation_id,
    expected_digest,
    control_message,
    reader,
    budgets=None,
    adopt=False,
):
    verify_control_identity(config, control_message.user_id, control_message.chat_id)
    row = conn.execute(
        "SELECT * FROM meeting_recovery_observations WHERE observation_id=? AND kind='check'",
        (observation_id,),
    ).fetchone()
    if row is None or row["payload_digest"] != expected_digest:
        raise MeetingRecoveryError("exact calendar observation not found")
    evidence = json.loads(row["payload_json"])
    attempt = _attempt(conn, row["attempt_id"])
    if attempt["phase"] in {"linked", "adopted"}:
        prior = conn.execute(
            "SELECT payload_json FROM meeting_recovery_observations WHERE attempt_id=? AND kind='binding' AND json_extract(payload_json,'$.observation_id')=?",
            (attempt["attempt_id"], observation_id),
        ).fetchone()
        if prior:
            return {
                "operation": attempt["phase"],
                "event_id": attempt["event_id"],
                "external_writes": False,
                "original_outcome_uncertain": attempt["phase"] == "adopted",
                "replayed": True,
            }
    allowed = {"known_created"} | (
        {"unique_candidate", "legacy_candidate"} if adopt else set()
    )
    if (
        evidence.get("classification") not in allowed
        or evidence["attempt_revision"] != attempt["revision"]
        or attempt["successor_preview_id"]
        or attempt["phase"] in {"never_dispatched", "conflict"}
    ):
        raise MeetingRecoveryError("calendar observation is not a current unique match")
    if parse_iso(evidence["checked_at"]) < datetime.now(UTC) - timedelta(minutes=5):
        raise MeetingRecoveryError("calendar observation expired; check again")
    fresh = check_meeting_creation(
        conn,
        config,
        preview_id=attempt["preview_id"],
        reader=reader,
        budgets=budgets,
        target_hint=evidence["target"]
        if evidence.get("target_basis") == "operator_search_scope"
        else None,
        control_message=control_message,
    )
    if (
        fresh["classification"] != evidence["classification"]
        or fresh["candidates"] != evidence["candidates"]
    ):
        raise MeetingRecoveryError("calendar changed since the displayed observation")
    with transaction(conn):
        current = _attempt(conn, attempt["attempt_id"])
        if (
            current["revision"] != attempt["revision"]
            or current["successor_preview_id"]
        ):
            raise MeetingRecoveryError("calendar attempt changed while binding")
        event_id = evidence["candidates"][0]["event_id"]
        target_encoded = canonical_json(evidence["target"])
        if conn.execute(
            "SELECT 1 FROM meeting_create_attempts WHERE attempt_id<>? AND coalesce(adopted_target_json,target_json)=? AND event_id=? AND phase IN ('linked','adopted','complete')",
            (attempt["attempt_id"], target_encoded, event_id),
        ).fetchone():
            raise MeetingRecoveryError(
                "calendar event is already bound to another operation"
            )
        _append(
            conn,
            attempt["attempt_id"],
            "binding",
            {
                "observation_id": observation_id,
                "fresh_observation_id": fresh["observation_id"],
                "event_id": event_id,
                "owner_user_id": control_message.user_id,
                "owner_chat_id": control_message.chat_id,
                "external_id": control_message.message_id,
                "provenance": "receipt_verified"
                if evidence["classification"] == "known_created"
                else "owner_adopted_existing_event",
            },
        )
        phase = "linked" if evidence["classification"] == "known_created" else "adopted"
        conn.execute(
            "UPDATE meeting_create_attempts SET phase=?,event_id=?,adopted_target_json=?,revision=revision+1,updated_at=? WHERE attempt_id=?",
            (phase, event_id, target_encoded, iso_now(), attempt["attempt_id"]),
        )
        conn.execute(
            "UPDATE meeting_previews SET status=?,calendar_event_id=?,remote_result_json=?,updated_at=? WHERE preview_id=?",
            (
                "created" if phase == "linked" else "creating",
                event_id,
                canonical_json(
                    {
                        "outcome": phase,
                        "event_id": event_id,
                        "original_outcome_uncertain": phase == "adopted",
                    }
                ),
                iso_now(),
                attempt["preview_id"],
            ),
        )
    return {
        "operation": phase,
        "event_id": event_id,
        "external_writes": False,
        "room_statuses": evidence["candidates"][0]["room_statuses"],
        "original_outcome_uncertain": phase == "adopted",
    }


def cancel_before_dispatch(
    conn, config, *, attempt_id, expected_revision, control_message
):
    verify_control_identity(config, control_message.user_id, control_message.chat_id)
    with transaction(conn):
        attempt = _attempt(conn, attempt_id)
        if (
            attempt["phase"] == "never_dispatched"
            and attempt["revision"] == expected_revision + 1
            and not attempt["successor_preview_id"]
        ):
            proof = conn.execute(
                "SELECT 1 FROM meeting_recovery_observations WHERE attempt_id=? AND kind='never_dispatched'",
                (attempt_id,),
            ).fetchone()
            if proof:
                return {
                    "phase": "never_dispatched",
                    "attempt_id": attempt_id,
                    "external_writes": False,
                    "replayed": True,
                }
        if attempt["phase"] != "prepared" or attempt["revision"] != expected_revision:
            raise MeetingRecoveryError("cannot prove this attempt was never dispatched")
        _append(
            conn,
            attempt_id,
            "never_dispatched",
            {
                "basis": "prepared_cancelled_by_atomic_dispatch_fence",
                "owner_user_id": control_message.user_id,
                "external_id": control_message.message_id,
            },
        )
        conn.execute(
            "UPDATE meeting_create_attempts SET phase='never_dispatched',revision=revision+1,updated_at=? WHERE attempt_id=?",
            (iso_now(), attempt_id),
        )
        conn.execute(
            "UPDATE meeting_previews SET status='failed',updated_at=? WHERE preview_id=?",
            (iso_now(), attempt["preview_id"]),
        )
    return {
        "phase": "never_dispatched",
        "attempt_id": attempt_id,
        "external_writes": False,
    }


def prepare_legacy_meeting_preview(
    conn, config, *, preview_id, expected_digest, control_message, runner
):
    """Revoke an unexecuted v1 action and create a new, fully bound v2 approval.

    The external operation here is only the owner/calendar identity read. Never
    use this path for an existing creating/uncertain/consumed legacy attempt.
    """
    from .calendar import exact_meeting_preview

    verify_control_identity(config, control_message.user_id, control_message.chat_id)
    preview, old_action = exact_meeting_preview(conn, preview_id)
    if (
        preview["action_digest"] != expected_digest
        or old_action.get("schema_version") == 2
    ):
        raise MeetingRecoveryError("legacy preview identity changed")
    existing = conn.execute(
        "SELECT * FROM meeting_create_attempts WHERE preview_id=?", (preview_id,)
    ).fetchone()
    if existing is not None:
        if existing["phase"] == "never_dispatched" and existing["successor_preview_id"]:
            successor = conn.execute(
                "SELECT * FROM meeting_previews WHERE preview_id=?",
                (existing["successor_preview_id"],),
            ).fetchone()
            return {
                "preview_id": successor["preview_id"],
                "approval_id": successor["approval_id"],
                "action_digest": successor["action_digest"],
                "action": json.loads(successor["action_json"]),
                "requires_approval": True,
                "external_writes": False,
                "replayed": True,
            }
        raise MeetingRecoveryError(
            "uncertain legacy creation cannot become a new approval"
        )
    if not config.feature("calendar"):
        raise MeetingRecoveryError("calendar previews are disabled")
    action = bind_meeting_action(config, old_action, runner=runner)
    with transaction(conn):
        preview, current = exact_meeting_preview(conn, preview_id)
        if (
            current != old_action
            or preview["approval_status"] not in {"requested", "approved"}
            or preview["consumed_at"] is not None
            or conn.execute(
                "SELECT 1 FROM meeting_create_attempts WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
        ):
            raise MeetingRecoveryError(
                "legacy approval was consumed or changed during target lookup"
            )
        if conn.execute(
            "SELECT 1 FROM meeting_create_attempts WHERE case_id=? AND phase NOT IN ('complete','linked','never_dispatched')",
            (preview["case_id"],),
        ).fetchone():
            raise MeetingRecoveryError(
                "an unresolved calendar attempt blocks a new approval"
            )
        now, attempt_id = iso_now(), new_id("mat")
        conn.execute(
            "UPDATE approvals SET status='revoked',updated_at=? WHERE approval_id=?",
            (now, preview["approval_id"]),
        )
        conn.execute(
            "UPDATE meeting_previews SET status='expired',updated_at=? WHERE preview_id=?",
            (now, preview_id),
        )
        conn.execute(
            "INSERT INTO meeting_create_attempts(attempt_id,preview_id,case_id,lifecycle_round,action_digest,action_json,phase,dispatch_token,created_at,updated_at) VALUES(?,?,?,?,?,?,'never_dispatched',?,?,?)",
            (
                attempt_id,
                preview_id,
                preview["case_id"],
                preview["current_round"],
                expected_digest,
                canonical_json(old_action),
                new_id("mdt"),
                now,
                now,
            ),
        )
        approval_id, successor_id, checksum = (
            new_id("apr"),
            new_id("mtg"),
            digest(action),
        )
        conn.execute(
            "INSERT INTO approvals(approval_id,approval_type,case_id,status,requested_action_json,action_digest,requested_at,expires_at,lifecycle_round,created_at,updated_at) VALUES(?,'meeting_create',?,'requested',?,?,?,?,?,?,?)",
            (
                approval_id,
                preview["case_id"],
                canonical_json(action),
                checksum,
                now,
                expiry_after(30),
                preview["current_round"],
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO meeting_previews(preview_id,case_id,action_json,action_digest,status,approval_id,created_at,updated_at) VALUES(?,?,?,?,'preview',?,?,?)",
            (
                successor_id,
                preview["case_id"],
                canonical_json(action),
                checksum,
                approval_id,
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE meeting_create_attempts SET successor_preview_id=?,revision=revision+1 WHERE attempt_id=?",
            (successor_id, attempt_id),
        )
        _append(
            conn,
            attempt_id,
            "never_dispatched",
            {
                "basis": "v1_approval_revoked_before_consumption",
                "owner_user_id": control_message.user_id,
                "external_id": control_message.message_id,
            },
        )
        _append(
            conn,
            attempt_id,
            "successor",
            {
                "preview_id": successor_id,
                "approval_id": approval_id,
                "action_digest": checksum,
                "owner_user_id": control_message.user_id,
                "external_id": control_message.message_id,
            },
        )
    return {
        "preview_id": successor_id,
        "approval_id": approval_id,
        "action_digest": checksum,
        "action": action,
        "requires_approval": True,
        "external_writes": False,
    }


def prepare_meeting_successor(
    conn, config, *, attempt_id, expected_revision, control_message
):
    from .calendar import exact_meeting_preview

    verify_control_identity(config, control_message.user_id, control_message.chat_id)
    with transaction(conn):
        attempt = _attempt(conn, attempt_id)
        if attempt["phase"] == "never_dispatched" and attempt["successor_preview_id"]:
            successor_row = conn.execute(
                "SELECT * FROM meeting_previews WHERE preview_id=?",
                (attempt["successor_preview_id"],),
            ).fetchone()
            if (
                successor_row is not None
                and attempt["revision"] == expected_revision + 1
            ):
                return {
                    "preview_id": successor_row["preview_id"],
                    "approval_id": successor_row["approval_id"],
                    "action_digest": successor_row["action_digest"],
                    "action": json.loads(successor_row["action_json"]),
                    "requires_approval": True,
                    "external_writes": False,
                    "replayed": True,
                }
        if (
            attempt["phase"] != "never_dispatched"
            or attempt["revision"] != expected_revision
            or attempt["successor_preview_id"]
        ):
            raise MeetingRecoveryError(
                "fresh approval requires an unused never-dispatched proof"
            )
        preview, action = exact_meeting_preview(conn, attempt["preview_id"])
        if not conn.execute(
            "SELECT 1 FROM meeting_recovery_observations WHERE attempt_id=? AND kind='never_dispatched'",
            (attempt_id,),
        ).fetchone():
            raise MeetingRecoveryError("never-dispatched proof missing")
        # This is one atomic reservation. The old token can never enter dispatch,
        # and another click cannot generate another authorized successor.
        successor = {**action, "operation_id": new_id("mop")[4:]}
        successor.pop("availability", None)
        now, approval_id, preview_id = iso_now(), new_id("apr"), new_id("mtg")
        checksum = digest(successor)
        conn.execute(
            "INSERT INTO approvals(approval_id,approval_type,case_id,status,requested_action_json,action_digest,requested_at,expires_at,lifecycle_round,created_at,updated_at) VALUES(?,'meeting_create',?,'requested',?,?,?,?,?,?,?)",
            (
                approval_id,
                attempt["case_id"],
                canonical_json(successor),
                checksum,
                now,
                expiry_after(30),
                preview["current_round"],
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO meeting_previews(preview_id,case_id,action_json,action_digest,status,approval_id,created_at,updated_at) VALUES(?,?,?,?,'preview',?,?,?)",
            (
                preview_id,
                attempt["case_id"],
                canonical_json(successor),
                checksum,
                approval_id,
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE meeting_create_attempts SET successor_preview_id=?,revision=revision+1,updated_at=? WHERE attempt_id=?",
            (preview_id, now, attempt_id),
        )
        _append(
            conn,
            attempt_id,
            "successor",
            {
                "preview_id": preview_id,
                "approval_id": approval_id,
                "action_digest": checksum,
                "owner_user_id": control_message.user_id,
                "external_id": control_message.message_id,
            },
        )
    return {
        "preview_id": preview_id,
        "approval_id": approval_id,
        "action_digest": checksum,
        "action": successor,
        "requires_approval": True,
        "external_writes": False,
    }
