from __future__ import annotations

import json
import shlex
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from .approvals import decide_approval, verify_control_identity
from .config import Config
from .coordination import ACTIVE_TURN_STATES, control_communication
from .db import atomic, transaction
from .ids import digest
from .runtime_control import capability_allowed, issue_global_panel
from .store import get_case, transition_case
from .timeutil import iso_now


class ControlError(ValueError):
    pass


@dataclass(frozen=True)
class ControlMessage:
    user_id: str
    chat_id: str
    message_id: str
    text: str
    source_card_message_id: str | None = None


_APPROVAL_GATES = {
    "board1_lease": "board",
    "wip_push": "push",
    "meeting_create": "meeting",
}


def _continue_after_delegate(
    conn: sqlite3.Connection, config: Config, *, case_id: str, actor_id: str
) -> dict[str, Any] | None:
    """Turn an owner-decision Case into the normal retrieve -> Codex path."""
    if not config.feature("codex") or not capability_allowed(conn, config, "codex"):
        return None
    from .retrieval import (
        RetrievalError,
        create_retrieval_job,
        retrieval_input_for_case,
    )

    try:
        current_input = retrieval_input_for_case(conn, case_id=case_id, project=True)
    except RetrievalError as exc:
        return {"state": "needs_context_review", "reason": str(exc), "created": False}
    source = {"event_pk": current_input["source_event_pk"]}
    query = current_input["full_query"]
    case = conn.execute(
        "SELECT state,version,lifecycle_round,owner FROM cases WHERE case_id=?",
        (case_id,),
    ).fetchone()
    if case is None or case["state"] in {"resolved", "takeover", "cancelled"}:
        return None
    current_turn = conn.execute(
        "SELECT turn_id,revision,fence,communication_owner,communication_mode,source_event_pk FROM conversation_turns WHERE case_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if case["owner"] != "hermes" or (
        current_turn
        and (
            current_turn["communication_owner"] != "ai"
            or current_turn["communication_mode"] != "respond"
        )
    ):
        return None
    if (
        current_turn is None
        or current_turn["source_event_pk"] != current_input["source_event_pk"]
    ):
        return {
            "state": "awaiting_current_input",
            "created": False,
            "reason": "newer input must be processed before delegated followup",
        }
    with transaction(conn):
        if retrieval_input_for_case(conn, case_id=case_id) != current_input:
            return {"state": "awaiting_current_input", "created": False}
        conn.execute(
            """UPDATE route_decisions SET route='codex_debug',review_status='accepted',
                   reviewed_by=?,reviewed_at=?,review_note='operator delegated Case to AI'
               WHERE route_decision_id=(
                   SELECT route_decision_id FROM route_decisions WHERE case_id=? AND event_pk=?
                   ORDER BY created_at DESC LIMIT 1
               ) AND route='owner_decision'""",
            (actor_id, iso_now(), case_id, current_input['source_event_pk']),
        )
    if case["state"] in {"escalated", "triage", "answering"}:
        transition_case(
            conn,
            case_id=case_id,
            after="investigating",
            actor_type="operator",
            actor_id=actor_id,
            reason="operator explicitly delegated a fresh investigation to AI",
            expected_version=int(case["version"]),
            idempotency_key=f"delegate:{case_id}:{source['event_pk']}:{case['lifecycle_round']}:investigate",
        )
    job_id, created = create_retrieval_job(
        conn,
        config,
        case_id=case_id,
        query=query,
        source_event_pk=str(source["event_pk"]),
        context_binding=current_input["context_binding"],
        request_generation=digest(
            {
                "reason": "explicit_operator_delegation",
                "case_id": case_id,
                "round": case["lifecycle_round"],
                "turn": dict(current_turn),
            }
        )
        if current_turn
        else None,
    )
    if created:
        with transaction(conn):
            conn.execute(
                """UPDATE cases SET next_action=?,version=version+1,updated_at=?
                   WHERE case_id=? AND lifecycle_round=? AND owner='hermes'
                     AND state='investigating'
                     AND EXISTS(SELECT 1 FROM delivery_blocks b WHERE b.case_id=cases.case_id
                       AND b.lifecycle_round=cases.lifecycle_round AND b.next_action=cases.next_action)
                     AND EXISTS(SELECT 1 FROM conversation_turns t WHERE t.turn_id=(
                       SELECT turn_id FROM conversation_turns WHERE case_id=cases.case_id
                       ORDER BY created_at DESC,rowid DESC LIMIT 1)
                       AND t.source_event_pk=? AND t.communication_owner='ai'
                       AND t.communication_mode='respond')""",
                (
                    "你已重新委托 AI；新查证已排队，旧答复不会自动重发。",
                    iso_now(),
                    case_id,
                    case["lifecycle_round"],
                    source["event_pk"],
                ),
            )
    return {"job_id": job_id, "created": created, "route": "codex_debug"}


def _stop_case_work(conn: sqlite3.Connection, *, case_id: str, reason: str) -> None:
    """Fence public writers, cancel queued work, and schedule board BROM cleanup."""
    now = iso_now()
    with atomic(conn):
        conn.execute(
            """UPDATE conversation_turns SET communication_owner='human',communication_mode='silent',
                   state='human_hold',fence=fence+1,updated_at=?
               WHERE case_id=? AND state IN ('open','ai_scheduled','ai_sending','human_hold')""",
            (now, case_id),
        )
        conn.execute(
            """UPDATE jobs SET state='cancelled',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
               WHERE case_id=? AND state='queued'""",
            (now, case_id),
        )
        conn.execute(
            """UPDATE jobs SET state='cancelled',error_class=?,lease_owner=NULL,
                   lease_expires_at=NULL,updated_at=?
               WHERE case_id=? AND state='running' AND job_type IN ('retrieve','codex')""",
            (reason, now, case_id),
        )
        conn.execute(
            """UPDATE outbox SET state='cancelled',suppression_reason=?,updated_at=?
               WHERE case_id=? AND channel='feishu_im' AND action_type IN ('reply','ack','clarify')
                 AND state IN ('pending','retry')""",
            (reason, now, case_id),
        )
        conn.execute(
            """UPDATE locks SET expires_at=?,heartbeat_at=?
               WHERE lock_key='board1' AND case_id=?""",
            (now, now, case_id),
        )


def _approval_row(conn: sqlite3.Connection, approval_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
    ).fetchone()
    if row is None or row["approval_type"] not in _APPROVAL_GATES:
        raise ControlError("approval not found or unsupported")
    return row


def _decide_and_continue(
    conn: sqlite3.Connection,
    config: Config,
    message: ControlMessage,
    *,
    approval_id: str,
    approve: bool,
    expected_digest: str,
    control_channel: str = "telegram",
    expected_binding: str | None = None,
) -> dict[str, Any]:
    row = _approval_row(conn, approval_id)
    gate = _APPROVAL_GATES[str(row["approval_type"])]
    approval = decide_approval(
        conn,
        config,
        approval_id=approval_id,
        approve=approve,
        approver_user_id=message.user_id,
        approver_chat_id=message.chat_id,
        message_id=message.message_id,
        decision_text=message.text,
        expected_digest=expected_digest,
        control_channel=control_channel,
        expected_binding=expected_binding,
    )
    if approval.get("cross_channel_replay"):
        return {"command": "approve" if approve else "deny", "gate": gate,
                "approval": approval, "replayed": True}
    if not approve:
        return {"command": "deny", "gate": gate, "approval": approval}
    result: dict[str, Any] = {
        "command": "approve",
        "gate": gate,
        "approval": approval,
    }
    if gate == "meeting" and control_channel in {"gui", "feishu"}:
        return {**result, "execution": {"state": "queued", "automatic_retry": False}}
    if (
        gate == "board"
        and config.feature("codex")
        and config.feature("board")
        and capability_allowed(conn, config, "board")
    ):
        from .review import continue_after_board_approval

        result["continuation"] = continue_after_board_approval(
            conn, config, approval_id=approval_id
        )
    if (
        gate == "push"
        and config.feature("codex")
        and config.feature("wip_push")
        and capability_allowed(conn, config, "wip_push")
    ):
        from .review import queue_after_push_approval

        result["continuation"] = queue_after_push_approval(
            conn, config, approval_id=approval_id
        )
    if (
        gate == "meeting"
        and config.feature("calendar")
        and capability_allowed(conn, config, "calendar")
    ):
        from functools import partial

        from .calendar import CalendarError, execute_meeting_create
        from .lark import run_json

        preview = conn.execute(
            "SELECT preview_id FROM meeting_previews WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if preview is None:
            raise ControlError("meeting approval has no exact preview")
        try:
            result["execution"] = execute_meeting_create(
                conn,
                config,
                preview_id=preview["preview_id"],
                runner=partial(run_json, executable=config.runtime("lark_cli_command")),
            )
        except CalendarError:
            if not conn.execute(
                "SELECT 1 FROM meeting_create_attempts WHERE preview_id=?",
                (preview["preview_id"],),
            ).fetchone():
                raise
            result["execution"] = {"requires_recovery": True, "automatic_retry": False}
        from .meeting_preview import recovery_panel
        from .meeting_recovery import meeting_recovery_report

        result["preview"] = recovery_panel(
            meeting_recovery_report(conn, config, preview_id=preview["preview_id"])
        )["preview"]
    return result


def execute_approval_callback(
    conn: sqlite3.Connection,
    config: Config,
    message: ControlMessage,
    *,
    action: str,
    approval_id: str,
    prompt_message_id: str,
) -> dict[str, Any]:
    """Resolve one Telegram button against the exact delivered prompt."""
    verify_control_identity(config, message.user_id, message.chat_id)
    if action not in {"approve", "deny", "details"}:
        raise ControlError("unsupported approval callback action")
    approval = _approval_row(conn, approval_id)
    matched = False
    for outbox in conn.execute(
        """SELECT payload_json,remote_message_id FROM outbox
             WHERE action_type='approval_request' AND case_id=? AND state='delivered'""",
        (approval["case_id"],),
    ):
        try:
            payload = json.loads(outbox["payload_json"])
        except (TypeError, ValueError):
            continue
        if payload.get("approval_id") == approval_id and str(
            outbox["remote_message_id"] or ""
        ) == str(prompt_message_id):
            matched = True
            break
    if not matched:
        raise ControlError("approval callback is not bound to this Telegram message")
    normalized = json.loads(approval["requested_action_json"])
    if action == "approve" and approval["approval_type"] == "meeting_create":
        raise ControlError("meeting requires its complete preview confirmation button")
    if action == "details":
        return {
            "command": "details",
            "gate": _APPROVAL_GATES[str(approval["approval_type"])],
            "approval": {
                "approval_id": approval_id,
                "case_id": approval["case_id"],
                "status": approval["status"],
                "expires_at": approval["expires_at"],
                "action": normalized,
            },
        }
    return _decide_and_continue(
        conn,
        config,
        message,
        approval_id=approval_id,
        approve=action == "approve",
        expected_digest=str(approval["action_digest"]),
    )


def execute_case_callback(
    conn: sqlite3.Connection,
    config: Config,
    message: ControlMessage,
    *,
    action: str,
    case_id: str,
    prompt_message_id: str,
) -> dict[str, Any]:
    """Resolve a Case-control button against the exact Telegram prompt."""
    operator_id = verify_control_identity(config, message.user_id, message.chat_id)
    if action not in {
        "claim",
        "suggest_only",
        "delegate",
        "takeover",
        "pause",
        "details",
    }:
        raise ControlError("unsupported Case callback action")
    callback_code = {
        "claim": "c",
        "suggest_only": "s",
        "delegate": "a",
        "takeover": "t",
        "pause": "p",
        "details": "i",
    }[action]
    expected_callback = f"k3c:{callback_code}:{case_id}"
    matched_payload: dict[str, Any] | None = None
    matched_round: int | None = None
    for outbox in conn.execute(
        """SELECT payload_json,remote_message_id,lifecycle_round FROM outbox
             WHERE case_id=? AND channel='telegram' AND state='delivered'""",
        (case_id,),
    ):
        try:
            payload = json.loads(outbox["payload_json"])
        except (TypeError, ValueError):
            continue
        if (
            payload.get("case_id") == case_id
            and str(outbox["remote_message_id"] or "") == str(prompt_message_id)
            and any(
                isinstance(button, dict)
                and button.get("callback_data") == expected_callback
                for button in (payload.get("buttons") or [])
            )
        ):
            matched_payload = payload
            matched_round = outbox["lifecycle_round"]
            break
    if matched_payload is None:
        raise ControlError("Case callback is not bound to this Telegram message")
    target_turn_id: str | None = None
    retargeted_from_turn_id: str | None = None
    if action != "details":
        current_round = conn.execute(
            "SELECT lifecycle_round FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if current_round is None or current_round["lifecycle_round"] != matched_round:
            raise ControlError("Case callback is stale for the current authority round")
        bound_turn_id = str(matched_payload.get("control_turn_id") or "")
        bound_turn = conn.execute(
            "SELECT turn_id,chat_id FROM conversation_turns WHERE case_id=? AND turn_id=?",
            (case_id, bound_turn_id),
        ).fetchone()
        current_turn = conn.execute(
            "SELECT turn_id,chat_id,fence FROM conversation_turns WHERE case_id=? ORDER BY created_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        exact = (
            current_turn is not None
            and bound_turn is not None
            and bound_turn_id == current_turn["turn_id"]
            and int(matched_payload.get("control_fence") or -1)
            == int(current_turn["fence"])
        )
        if exact and current_turn is not None:
            target_turn_id = str(current_turn["turn_id"])
        elif action in {"claim", "suggest_only"} and bound_turn is not None:
            placeholders = ",".join("?" for _ in ACTIVE_TURN_STATES)
            active_turn = conn.execute(
                f"""SELECT turn_id,chat_id FROM conversation_turns
                       WHERE case_id=? AND state IN ({placeholders})
                       ORDER BY created_at DESC LIMIT 1""",
                (case_id, *sorted(ACTIVE_TURN_STATES)),
            ).fetchone()
            if (
                active_turn is not None
                and active_turn["chat_id"] == bound_turn["chat_id"]
            ):
                target_turn_id = str(active_turn["turn_id"])
                retargeted_from_turn_id = bound_turn_id
            else:
                raise ControlError(
                    "Case callback is stale for the current conversation turn"
                )
        else:
            # Increasing AI authority from an old card is never retargeted.
            raise ControlError(
                "Case callback is stale for the current conversation turn"
            )
    if action in {"claim", "suggest_only", "delegate", "details"}:
        result = control_communication(
            conn,
            case_id=case_id,
            action=action,
            actor_id=operator_id,
            external_id=f"telegram-callback:{message.message_id}",
            turn_id=target_turn_id,
        )
        if retargeted_from_turn_id is not None:
            result["retargeted_from_turn_id"] = retargeted_from_turn_id
            result["retargeted_to_turn_id"] = target_turn_id
        if action == "delegate" and not result.get("replayed"):
            result["continuation"] = _continue_after_delegate(
                conn, config, case_id=case_id, actor_id=operator_id
            )
        return result
    current = get_case(conn, case_id)
    after = "takeover" if action == "takeover" else "paused"
    version = transition_case(
        conn,
        case_id=case_id,
        after=after,
        actor_type="operator",
        actor_id=operator_id,
        reason=f"Telegram button {action}",
        expected_version=int(current["version"]),
        idempotency_key=f"telegram-callback:{message.message_id}",
    )
    _stop_case_work(conn, case_id=case_id, reason=f"case_{action}")
    return {"command": action, "case_id": case_id, "state": after, "version": version}


def _execute_control(
    conn: sqlite3.Connection,
    config: Config,
    message: ControlMessage,
    *,
    control_channel: str = "telegram",
) -> dict[str, Any]:
    operator_id = verify_control_identity(config, message.user_id, message.chat_id, channel=control_channel)
    try:
        argv = shlex.split(message.text)
    except ValueError as exc:
        raise ControlError("invalid command quoting") from exc
    if not argv:
        raise ControlError("empty command")
    command = argv[0].lower()
    if control_channel not in {"telegram", "gui", "feishu"} or (
        control_channel == "gui" and command != "case-action"
    ):
        raise ControlError("unsupported control channel or command")
    if control_channel == "feishu":
        if command == "card-bind" and len(argv) == 3:
            from .feishu_card_bindings import bind
            return bind(conn, message, argv[1], argv[2])
        if command == "card-action" and len(argv) == 3:
            from .feishu_card_bindings import resolve
            text = resolve(conn, message, argv[1], argv[2])
            return _execute_control(conn, config, replace(message, text=text), control_channel="feishu")
        if command == "/feishu" and len(argv) == 1:
            command, argv = "mode", ["mode"]
        allowed = {"bug", "pause", "resume", "takeover", "mode", "mode-action", "workbench", "workbench-nav", "status", "claim", "suggest-only", "delegate", "case-action", "approval", "approve", "deny"}
        if command not in allowed:
            raise ControlError("unsupported Feishu control command")
        if command in {"mode", "mode-action"}:
            from .feishu_control import route_mode
            return route_mode(conn, config, message, argv)
        if command == "approval" and len(argv) == 2:
            from .approvals import approval_binding
            row = _approval_row(conn, argv[1])
            binding = approval_binding(conn, argv[1])
            return {"command": "approval_detail", "approval": dict(row), "binding": binding,
                    "text": str(row["requested_action_json"]),
                    "commands": [f"{action} {row['approval_id']} {row['action_digest']} {binding}" for action in ("approve", "deny")]}
        if command in {"approve", "deny"}:
            if len(argv) != 4:
                raise ControlError("Feishu approval requires exact digest and fresh details binding")
            return _decide_and_continue(conn, config, message, approval_id=argv[1], approve=command == "approve",
                                        expected_digest=argv[2], expected_binding=argv[3], control_channel="feishu")
    if command == "bug":
        from .project_chat_control import route
        result = route(conn, config, message, [command, *argv[1:]], channel=control_channel)
        if "delegate_control" in result:
            return _execute_control(conn, config, replace(message, text=result["delegate_control"]), control_channel=control_channel)
        return result
    if command == "workbench-nav":
        from .workbench_navigation import route

        if len(argv) != 2:
            raise ControlError("usage: workbench-nav <callback_data>")
        return route(conn, config, argv[1])
    if command == "mail-view":
        from .mail_preview import route

        if len(argv) != 3:
            raise ControlError("usage: mail-view <prompt_message_id> <callback_data>")
        return route(
            conn,
            config,
            user_id=message.user_id,
            chat_id=message.chat_id,
            prompt_message_id=argv[1],
            callback_data=argv[2],
            external_id=f"{control_channel}:{message.message_id}",
        )
    if command == "meeting-view":
        from .meeting_preview import route

        if len(argv) != 3:
            raise ControlError(
                "usage: meeting-view <prompt_message_id> <callback_data>"
            )
        return route(
            conn, config, message, prompt_message_id=argv[1], callback_data=argv[2]
        )
    if command == "remote-recovery":
        from .remote_cleanup_control import route

        if len(argv) != 3:
            raise ControlError("usage: remote-recovery <prompt_message_id> <callback_data>")
        return route(conn, config, message, prompt_message_id=argv[1], callback_data=argv[2])
    if command == "meeting-recovery":
        from .meeting_recovery_control import route

        if len(argv) != 3:
            raise ControlError(
                "usage: meeting-recovery <prompt_message_id> <callback_data>"
            )
        return route(
            conn, config, message, prompt_message_id=argv[1], callback_data=argv[2]
        )
    if command == "meeting-recovery-scope":
        from .meeting_recovery_control import search_legacy_scope

        if len(argv) != 4:
            raise ControlError(
                "usage: meeting-recovery-scope <prompt_message_id> <preview_id> <calendar_id>"
            )
        return search_legacy_scope(
            conn,
            config,
            message,
            prompt_message_id=argv[1],
            preview_id=argv[2],
            calendar_id=argv[3],
        )
    if command in {"resolve", "reopen"}:
        from .lifecycle import operator_transition

        if len(argv) < 3 or not argv[2].isdigit():
            raise ControlError(f"usage: {command} <case> <expected_version> [reason]")
        return operator_transition(
            conn,
            case_id=argv[1],
            action=command,
            expected_version=int(argv[2]),
            actor_id=operator_id,
            idempotency_key=f"{control_channel}:{message.message_id}",
            reason=" ".join(argv[3:]),
        )
    if command == "case-action":
        from .case_actions import action_binding
        from .case_detail import case_detail
        from .lifecycle import LifecycleError, _atomic, operator_transition

        if len(argv) not in {4, 6} or (len(argv) == 6 and argv[4] != "return"):
            raise ControlError("stale Case action format; refresh Case details")
        action, case_id = argv[1:3]
        with _atomic(conn):
            from .workbench_navigation import decode, validate_origin

            origin = validate_origin(conn, argv[5] if len(argv) == 6 else "wb2:open:a")
            navigation = decode(conn, origin)[0]
            target = conn.execute(
                "SELECT item_seq FROM workbench_item_keys WHERE entity_kind='case' AND target_key=?",
                (case_id,),
            ).fetchone()
            if target is None or target[0] > navigation.upper:
                raise ControlError(
                    "Case is outside workbench browsing range; refresh Case details"
                )
            binding = action_binding(conn, case_id=case_id, action=action)
            if argv[3] != binding["token"]:
                raise LifecycleError(
                    "stale Case lifecycle control; refresh Case details"
                )
            version, round_number, fence = (
                binding["version"],
                binding["round"],
                binding["fence"],
            )
            if action in {"resolve", "reopen"}:
                result = operator_transition(
                    conn,
                    case_id=case_id,
                    action=action,
                    expected_version=version,
                    actor_id=operator_id,
                    idempotency_key=f"{control_channel}:{message.message_id}",
                    reason=f"{control_channel} Case detail button",
                )
            elif action in {"claim", "delegate", "suggest_only"}:
                result = control_communication(
                    conn,
                    case_id=case_id,
                    action=action,
                    actor_id=operator_id,
                    external_id=f"{control_channel}:{message.message_id}",
                    expected_case_version=version,
                    expected_round=round_number,
                    expected_fence=fence,
                    turn_id=binding["turn_id"],
                )
            else:
                raise ControlError("unsupported Case lifecycle action")
        if action == "delegate" and not result.get("replayed"):
            result["continuation"] = _continue_after_delegate(
                conn, config, case_id=case_id, actor_id=operator_id
            )
        return {
            **result,
            "preview": case_detail(conn, case_id=case_id, origin_cursor=origin)[
                "preview"
            ],
        }
    if command == "workbench":
        from .workbench import workbench_panel

        try:
            if len(argv) == 1:
                return workbench_panel(conn, config)
        except ValueError as exc:
            raise ControlError(str(exc)) from exc
        raise ControlError(
            "legacy workbench navigation is unsupported; refresh the workbench"
        )
    if command == "/feishu" and len(argv) == 1:
        return issue_global_panel(
            conn,
            config,
            operator_user_id=message.user_id,
            chat_id=message.chat_id,
            command_message_id=message.message_id,
        )
    if command == "status" and len(argv) == 2:
        return {"command": "status", "case": get_case(conn, argv[1])}
    if command in {"claim", "suggest-only", "delegate"} and len(argv) == 2:
        action = {
            "claim": "claim",
            "suggest-only": "suggest_only",
            "delegate": "delegate",
        }[command]
        result = control_communication(
            conn,
            case_id=argv[1],
            action=action,
            actor_id=operator_id,
            external_id=f"{control_channel}:{message.message_id}",
        )
        if action == "delegate" and not result.get("replayed"):
            result["continuation"] = _continue_after_delegate(
                conn, config, case_id=argv[1], actor_id=operator_id
            )
        return result
    if command in {"pause", "resume", "takeover", "cancel"}:
        if len(argv) < 3 or not argv[2].isdigit():
            raise ControlError(f"usage: {command} <case> <expected_version> [reason]")
        case_id, expected_version = argv[1], int(argv[2])
        reason = " ".join(argv[3:]) or f"{control_channel} {command}"
        with atomic(conn):
            previous = conn.execute(
                "SELECT * FROM case_events WHERE case_id=? AND idempotency_key=?",
                (case_id, f"{control_channel}:{message.message_id}"),
            ).fetchone()
            if previous:
                desired = {"pause": "paused", "takeover": "takeover", "cancel": "cancelled"}.get(command)
                if (previous["actor_id"] != operator_id
                        or json.loads(previous["detail_json"]).get("reason") != reason
                        or desired is not None and previous["after_state"] != desired
                        or command == "resume" and previous["before_state"] != "paused"):
                    raise ControlError("control message identity reused with different action")
                current = get_case(conn, case_id)
                return {"command": command, "case_id": case_id, "state": current["state"],
                        "version": current["version"], "replayed": True}
            current = get_case(conn, case_id)
            if command == "resume":
                if current["state"] != "paused":
                    raise ControlError("resume requires paused state")
                pause_event = next(
                    (
                        event
                        for event in current["events"]
                        if event["after_state"] == "paused"
                    ),
                    None,
                )
                if pause_event is None:
                    raise ControlError("pre-pause state is unavailable")
                after = pause_event["before_state"]
            else:
                after = {"pause": "paused", "takeover": "takeover", "cancel": "cancelled"}[
                    command
                ]
            version = transition_case(
                conn,
                case_id=case_id,
                after=after,
                actor_type="operator",
                actor_id=operator_id,
                reason=reason,
                expected_version=expected_version,
                idempotency_key=f"{control_channel}:{message.message_id}",
            )
            if command in {"pause", "takeover", "cancel"}:
                _stop_case_work(conn, case_id=case_id, reason=f"case_{command}")
            return {
                "command": command,
                "case_id": case_id,
                "state": after,
                "version": version,
            }
    if command == "approve" and len(argv) == 2:
        row = _approval_row(conn, argv[1])
        return _decide_and_continue(
            conn,
            config,
            message,
            approval_id=argv[1],
            approve=True,
            expected_digest=str(row["action_digest"]),
        )
    if (
        command == "approve"
        and len(argv) == 4
        and argv[1]
        in {
            "board",
            "push",
            "meeting",
        }
    ):
        approval_id, expected_digest = argv[2], argv[3]
        row = conn.execute(
            "SELECT approval_type FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        expected_type = {
            "board": "board1_lease",
            "push": "wip_push",
            "meeting": "meeting_create",
        }[argv[1]]
        if row is None or row[0] != expected_type:
            raise ControlError("approval type does not match command")
        return _decide_and_continue(
            conn,
            config,
            message,
            approval_id=approval_id,
            approve=True,
            expected_digest=expected_digest,
        )
    if command == "deny" and len(argv) == 2:
        row = _approval_row(conn, argv[1])
        return _decide_and_continue(
            conn,
            config,
            message,
            approval_id=argv[1],
            approve=False,
            expected_digest=str(row["action_digest"]),
        )
    if command == "deny" and len(argv) == 3:
        approval_id, expected_digest = argv[1], argv[2]
        return _decide_and_continue(
            conn,
            config,
            message,
            approval_id=approval_id,
            approve=False,
            expected_digest=expected_digest,
        )
    if command == "knowledge" and len(argv) >= 4 and argv[1].lower() == "feedback":
        verdict, target = argv[2].lower(), argv[3]
        from .knowledge import record_feedback

        result = record_feedback(
            conn,
            verdict=verdict,
            actor_id=operator_id,
            case_id=target if target.startswith("K3-") else None,
            knowledge_id=target if target.startswith("knw_") else None,
            detail=" ".join(argv[4:]) or None,
        )
        row = conn.execute(
            "SELECT knowledge_id,title,status FROM knowledge_entries WHERE knowledge_id=?",
            (result["knowledge_id"],),
        ).fetchone()
        return {
            "command": "knowledge",
            "operation": f"feedback:{verdict}",
            "knowledge": dict(row),
            "feedback": result,
        }
    if command == "knowledge" and len(argv) in {3, 5} and argv[1].lower() == "show":
        from .knowledge_preview import KnowledgePreviewError, knowledge_preview

        if len(argv) == 5 and not argv[3].isdigit():
            raise ControlError(
                "usage: knowledge show <knowledge_id> [page content_digest]"
            )
        try:
            return knowledge_preview(
                conn,
                knowledge_id=argv[2],
                page=int(argv[3]) if len(argv) == 5 else 1,
                expected_digest=argv[4] if len(argv) == 5 else None,
            )
        except KnowledgePreviewError as exc:
            raise ControlError(str(exc)) from exc
    if command == "knowledge" and len(argv) == 3:
        operation, knowledge_id = argv[1].lower(), argv[2]
        if not knowledge_id.startswith("knw_"):
            raise ControlError("knowledge command requires a stable knowledge ID")
        decisions = {"approve": "approved", "return": "candidate", "retire": "retired"}
        if operation not in decisions:
            raise ControlError(
                "usage: knowledge <show|approve|return|retire> <knowledge_id>"
            )
        from .knowledge import review

        review(
            conn,
            knowledge_id=knowledge_id,
            reviewer_id=message.user_id,
            decision=decisions[operation],
        )
        row = conn.execute(
            "SELECT knowledge_id,title,status,reviewed_by,reviewed_at FROM knowledge_entries WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()
        return {
            "command": "knowledge",
            "operation": operation,
            "knowledge": dict(row),
        }
    raise ControlError("unsupported control command")


def execute_control(conn, config, message, *, control_channel="telegram", text_only=False):
    result = _execute_control(conn, config, message, control_channel=control_channel)
    if control_channel == "feishu" and not text_only:
        from .feishu_card_bindings import issue
        return issue(conn, message, result)
    return result
