"""Authenticated phone recovery over the original, delivered meeting preview.

UI coordinates select immutable local records. They never carry a replacement
calendar/event target, confer authority, or authorize an external calendar write.
"""

from __future__ import annotations

import re
from functools import partial

from .approvals import ApprovalError, verify_control_identity
from .calendar import queue_reserved_meeting_preview
from .lark import run_json
from .meeting_preview import _context, recovery_panel
from .meeting_recovery import (
    CalendarReader,
    RecoveryBudget,
    bind_existing_meeting,
    cancel_before_dispatch,
    check_meeting_creation,
    meeting_recovery_report,
    prepare_legacy_meeting_preview,
    prepare_meeting_successor,
)


def route(conn, config, message, *, prompt_message_id, callback_data, runner=None):
    result = _route(conn, config, message, prompt_message_id=prompt_message_id,
                    callback_data=callback_data, runner=runner)
    return {**result, 'display_target': {'kind': 'edit_original', 'chat_id': message.chat_id,
                                        'message_id': prompt_message_id}}


def _route(conn, config, message, *, prompt_message_id, callback_data, runner=None):
    verify_control_identity(config, message.user_id, message.chat_id)
    match = re.fullmatch(
        r"mr:([vcxnabu]):([a-f0-9]{32}):([a-f0-9.]{1,24})", callback_data
    )
    if not match or len(callback_data.encode()) > 64:
        raise ApprovalError("invalid meeting recovery callback")
    operation, identifier, coordinate = match.groups()
    observation = None
    if operation in {"a", "b"}:
        observation = conn.execute(
            """SELECT o.*,a.preview_id FROM meeting_recovery_observations o
            JOIN meeting_create_attempts a USING(attempt_id)
            WHERE o.observation_id=? AND o.kind='check'""",
            ("mro_" + identifier,),
        ).fetchone()
        if observation is None or coordinate != observation["payload_digest"][:16]:
            raise ApprovalError("meeting recovery observation changed; refresh")
        preview_id = observation["preview_id"]
    else:
        preview_id = "mtg_" + identifier
    # A valid owner + unrelated meeting card must not select this target.
    _context(conn, config, message, prompt_message_id, preview_id)
    report = meeting_recovery_report(conn, config, preview_id=preview_id)
    if operation == "v":
        position = re.fullmatch(r"([1-9][0-9]{0,4})\.([0-9]{1,19})", coordinate)
        if not position or int(position[2]) != report.get("revision", 0):
            raise ApprovalError("meeting recovery changed; reopen the original preview")
        return recovery_panel(report, page=int(position[1]))
    # Action coordinates must be exactly one of the current rendered buttons,
    # not a guessed verb or a previous attempt's observation.
    first = recovery_panel(report)
    last = recovery_panel(report, page=first["preview"]["page_count"])
    allowed = {
        button["callback_data"]
        for panel in (first, last)
        for button in panel["preview"]["buttons"]
    }
    replay = False
    if operation in {"x", "n"} and coordinate.isdigit():
        replay = (
            report["phase"] == "never_dispatched"
            and report.get("revision") == int(coordinate) + 1
            and bool(report.get("successor_preview_id")) == (operation == "n")
        )
    elif operation == "u":
        replay = (
            report["phase"] == "never_dispatched"
            and bool(report.get("successor_preview_id"))
            and report["action"].get("schema_version") != 2
            and coordinate == report["action_digest"][:16]
        )
    elif operation in {"a", "b"}:
        replay = report["phase"] == ("adopted" if operation == "a" else "linked")
    if callback_data not in allowed and not replay:
        raise ApprovalError("meeting recovery action is stale or unavailable; refresh")
    runner = runner or partial(run_json, executable=config.runtime("lark_cli_command"))
    reader = CalendarReader(runner)
    if operation == "c":
        result = check_meeting_creation(
            conn, config, preview_id=preview_id, reader=reader, control_message=message,
            budgets=RecoveryBudget(deadline_seconds=5.0),
        )
    elif operation in {"a", "b"}:
        result = bind_existing_meeting(
            conn,
            config,
            observation_id=observation["observation_id"],
            expected_digest=observation["payload_digest"],
            control_message=message,
            reader=reader,
            adopt=operation == "a",
            budgets=RecoveryBudget(deadline_seconds=5.0),
        )
    elif operation == "x":
        result = cancel_before_dispatch(
            conn,
            config,
            attempt_id=report["attempt_id"],
            expected_revision=int(coordinate),
            control_message=message,
        )
    elif operation == "n":
        result = prepare_meeting_successor(
            conn,
            config,
            attempt_id=report["attempt_id"],
            expected_revision=int(coordinate),
            control_message=message,
        )
    else:
        result = prepare_legacy_meeting_preview(
            conn,
            config,
            preview_id=preview_id,
            expected_digest=report["action_digest"],
            control_message=message,
            runner=runner,
        )
    if operation in {"n", "u"}:
        # The helper atomically reserves the successor. This only makes its new
        # approval visible; a queue crash can be retried without a second create.
        queued = queue_reserved_meeting_preview(conn, config, preview_id=result['preview_id'],
                                                expected_digest=result['action_digest'])
        return {
            "command": "meeting_recovery",
            "operation": "new_preview_queued",
            "result": result,
            "successor": queued,
            "preview": {
                "text": "<b>新的完整审批已排队</b>\n原动作已不能创建；请在新卡查看并确认。没有创建日程或发邀请。",
                "parse_mode": "HTML",
                "page": 1,
                "page_count": 1,
                "buttons": [],
            },
        }
    return {
        **recovery_panel(meeting_recovery_report(conn, config, preview_id=preview_id)),
        "result": result,
    }


def search_legacy_scope(
    conn, config, message, *, prompt_message_id, preview_id, calendar_id, runner=None
):
    """Explicit owner-selected search scope, not a recovered original target."""
    _context(conn, config, message, prompt_message_id, preview_id)
    target = {
        "calendar_id": calendar_id,
        "actor_open_id": config.raw["identity"].get("feishu_owner_open_id"),
        "identity": "user",
        "user_id_type": "open_id",
    }
    runner = runner or partial(run_json, executable=config.runtime("lark_cli_command"))
    result = check_meeting_creation(
        conn,
        config,
        preview_id=preview_id,
        reader=CalendarReader(runner),
        budgets=RecoveryBudget(deadline_seconds=5.0),
        target_hint=target,
        control_message=message,
    )
    return {
        **recovery_panel(meeting_recovery_report(conn, config, preview_id=preview_id)),
        "result": result,
        'display_target': {'kind': 'edit_original', 'chat_id': message.chat_id,
                           'message_id': prompt_message_id},
    }
