from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .db import transaction
from .ids import new_id
from .timeutil import epoch_now, iso_now, parse_iso, utc_now


class RuntimeControlError(RuntimeError):
    pass


SCOPE = "feishu_support"
MODES = {"observe", "collaborate", "auto_60", "auto", "paused", "stopped"}
MODE_LABELS = {
    "observe": "观察",
    "collaborate": "协作",
    "auto_60": "自动 60 分钟",
    "auto": "自动",
    "paused": "立即暂停",
    "stopped": "完全停止",
}
PUBLIC_ACTIONS = {"reply", "ack", "clarify"}
AUTO_SECONDS = 60 * 60
CONFIRM_SECONDS = 60


def _tx(conn: sqlite3.Connection):
    return nullcontext(conn) if conn.in_transaction else transaction(conn)


def _cancel_public_outbox(conn: sqlite3.Connection, reason: str) -> int:
    affected = conn.execute(
        """SELECT DISTINCT turn_id,case_id FROM outbox
             WHERE channel='feishu_im' AND action_type IN ('reply','ack','clarify')
               AND state IN ('pending','retry','sending')"""
    ).fetchall()
    cursor = conn.execute(
        """UPDATE outbox SET state='cancelled',suppression_reason=?,lease_owner=NULL,
                  lease_expires_at=NULL,updated_at=?
             WHERE channel='feishu_im' AND action_type IN ('reply','ack','clarify')
               AND state IN ('pending','retry','sending')""",
        (reason, iso_now()),
    )
    now = iso_now()
    turn_ids = sorted(
        {str(row["turn_id"]) for row in affected if row["turn_id"] is not None}
    )
    if turn_ids:
        placeholders = ",".join("?" for _ in turn_ids)
        conn.execute(
            f"""UPDATE conversation_turns
                   SET communication_owner='human',communication_mode='silent',
                       state='human_hold',fence=fence+1,updated_at=?
                 WHERE turn_id IN ({placeholders})
                   AND state IN ('ai_scheduled','ai_sending')""",
            (now, *turn_ids),
        )
    case_ids = sorted(
        {str(row["case_id"]) for row in affected if row["case_id"] is not None}
    )
    for case_id in case_ids:
        case = conn.execute(
            "SELECT state,version FROM cases WHERE case_id=?", (case_id,)
        ).fetchone()
        if case is None or case["state"] != "answering":
            continue
        conn.execute(
            """UPDATE cases SET state='investigating',version=version+1,
                      next_action=?,updated_at=?,updated_epoch=?
                 WHERE case_id=? AND version=?""",
            (
                "Global mode changed; reply requires fresh review or delegation",
                now,
                epoch_now(),
                case_id,
                case["version"],
            ),
        )
        sequence = conn.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM case_events WHERE case_id=?",
            (case_id,),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO case_events(
                   event_id,case_id,sequence,event_type,actor_type,before_state,
                   after_state,detail_json,idempotency_key,created_at,created_epoch)
               VALUES(?,?,?,'communication_fenced','system','answering',
                      'investigating',?,?,?,?)""",
            (
                new_id("cev"),
                case_id,
                sequence,
                json.dumps({"reason": reason}, ensure_ascii=False),
                f"global-fence:{case_id}:{reason}:{case['version']}",
                now,
                epoch_now(),
            ),
        )
    return int(cursor.rowcount)


def _expire_board_lock(conn: sqlite3.Connection) -> int:
    now = iso_now()
    cursor = conn.execute(
        "UPDATE locks SET expires_at=?,heartbeat_at=? WHERE lock_key='board1' AND expires_at>?",
        (now, now, now),
    )
    return int(cursor.rowcount)


def _insert_initial_state(
    conn: sqlite3.Connection, *, actor_id: str, source: str, external_id: str
) -> sqlite3.Row:
    now = iso_now()
    # Fence 2 invalidates rows created before the runtime controller was enabled.
    conn.execute(
        """INSERT INTO global_control_state(scope,mode,revision,outbound_fence,
               changed_by,change_source,changed_at)
           VALUES(?, 'observe',1,2,?,?,?)""",
        (SCOPE, actor_id, source, now),
    )
    conn.execute(
        """INSERT INTO global_control_events(event_id,scope,external_id,before_mode,
               after_mode,before_revision,after_revision,actor_id,source,reason,created_at)
           VALUES(?,?,?,NULL,'observe',NULL,1,?,?,?,?)""",
        (
            new_id("gce"),
            SCOPE,
            external_id,
            actor_id,
            source,
            "runtime controller initialized in safe observe mode",
            now,
        ),
    )
    _cancel_public_outbox(conn, "global_controller_initialized")
    return conn.execute(
        "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
    ).fetchone()


def ensure_global_state(
    conn: sqlite3.Connection,
    *,
    actor_id: str = "system",
    source: str = "runtime",
    external_id: str | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
    ).fetchone()
    if row is None:
        with _tx(conn):
            row = conn.execute(
                "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
            ).fetchone()
            if row is None:
                row = _insert_initial_state(
                    conn,
                    actor_id=actor_id,
                    source=source,
                    external_id=external_id or f"initialize:{new_id('ctl')}",
                )
    return dict(row)


def _transition(
    conn: sqlite3.Connection,
    *,
    after: str,
    actor_id: str,
    source: str,
    external_id: str,
    reason: str,
    expected_revision: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if after not in MODES:
        raise RuntimeControlError("unsupported global mode")
    observed = now or utc_now()
    current = conn.execute(
        "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
    ).fetchone()
    if current is None:
        raise RuntimeControlError("global control state is not initialized")
    replay = conn.execute(
        "SELECT after_revision FROM global_control_events WHERE external_id=?",
        (external_id,),
    ).fetchone()
    if replay is not None:
        row = conn.execute(
            "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
        ).fetchone()
        return {**dict(row), "replayed": True}
    if int(current["revision"]) != expected_revision:
        raise RuntimeControlError("global control panel is stale")
    if current["mode"] == after and after != "auto_60":
        return {**dict(current), "replayed": False, "changed": False}
    if current["mode"] == "stopped" and after != "observe":
        raise RuntimeControlError("完全停止后必须先切换到观察模式")
    expiry = (
        (observed + timedelta(seconds=AUTO_SECONDS)).isoformat()
        if after == "auto_60"
        else None
    )
    revision = expected_revision + 1
    changed_at = observed.isoformat()
    changed = conn.execute(
        """UPDATE global_control_state SET mode=?,revision=?,outbound_fence=outbound_fence+1,
                  auto_expires_at=?,changed_by=?,change_source=?,changed_at=?
             WHERE scope=? AND revision=?""",
        (
            after,
            revision,
            expiry,
            actor_id,
            source,
            changed_at,
            SCOPE,
            expected_revision,
        ),
    )
    if changed.rowcount != 1:
        raise RuntimeControlError("global control mode changed concurrently")
    conn.execute(
        """INSERT INTO global_control_events(event_id,scope,external_id,before_mode,
               after_mode,before_revision,after_revision,actor_id,source,reason,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("gce"),
            SCOPE,
            external_id,
            current["mode"],
            after,
            expected_revision,
            revision,
            actor_id,
            source,
            reason,
            changed_at,
        ),
    )
    cancelled = _cancel_public_outbox(conn, f"global_mode_changed_to_{after}")
    board_cleanup = _expire_board_lock(conn) if after in {"paused", "stopped"} else 0
    cancelled_jobs = 0
    if after == "stopped":
        cancelled_jobs = int(
            conn.execute(
                """UPDATE jobs SET state='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                          error_class='global_stopped',updated_at=? WHERE state='queued'""",
                (changed_at,),
            ).rowcount
        )
        conn.execute(
            """UPDATE outbox SET state='cancelled',suppression_reason='global_stopped',
                      lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                 WHERE state IN ('pending','retry','sending')""",
            (changed_at,),
        )
    row = conn.execute(
        "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
    ).fetchone()
    if source == "timer":
        conn.execute(
            """UPDATE global_control_panels SET expected_global_revision=?,updated_at=?
                 WHERE scope=? AND state='active' AND expected_global_revision=?""",
            (revision, changed_at, SCOPE, expected_revision),
        )
    return {
        **dict(row),
        "replayed": False,
        "cancelled_public_outbox": cancelled,
        "cancelled_jobs": cancelled_jobs,
        "board_cleanup_requested": bool(board_cleanup),
    }


def current_global_state(
    conn: sqlite3.Connection,
    config: Config | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return effective state; lazily expire auto_60 to collaborate.

    Before the controller is explicitly initialized, retain the legacy static
    behavior. Deployment initializes the controller in observe mode before
    workflow services are started.
    """
    row = conn.execute(
        "SELECT * FROM global_control_state WHERE scope=?", (SCOPE,)
    ).fetchone()
    if row is None:
        legacy = "auto" if config is not None and config.mode == "active" else "observe"
        return {
            "scope": SCOPE,
            "mode": legacy,
            "revision": 0,
            "outbound_fence": 1,
            "auto_expires_at": None,
            "initialized": False,
        }
    value = dict(row)
    value["initialized"] = True
    observed = now or utc_now()
    if value["mode"] == "auto_60" and parse_iso(value["auto_expires_at"]) <= observed:
        with _tx(conn):
            value = _transition(
                conn,
                after="collaborate",
                actor_id="system",
                source="timer",
                external_id=f"auto60-expiry:{value['revision']}",
                reason="automatic 60 minute window expired",
                expected_revision=int(value["revision"]),
                now=observed,
            )
            value["initialized"] = True
    return value


def capability_allowed(
    conn: sqlite3.Connection,
    config: Config,
    capability: str,
    *,
    turn_id: str | None = None,
) -> bool:
    state = current_global_state(conn, config)
    mode = str(state["mode"])
    if capability == "ingest":
        return mode != "stopped"
    if capability in {"triage", "retrieve"}:
        return mode in {"observe", "collaborate", "auto_60", "auto"}
    if capability in {"codex", "board", "wip_push", "calendar", "base_sync"}:
        return config.mode == "active" and mode in {"collaborate", "auto_60", "auto"}
    if capability == "operator_prompt":
        return mode in {"collaborate", "auto_60", "auto"}
    if capability == "p0_alert":
        return mode != "stopped"
    if capability == "outbound_any":
        return config.mode == "active" and mode != "stopped"
    if capability == "public_reply":
        if config.mode != "active":
            return False
        if mode in {"auto_60", "auto"}:
            return True
        if mode != "collaborate" or not turn_id:
            return False
        row = conn.execute(
            """SELECT 1 FROM operator_activities
                 WHERE matched_turn_id=? AND activity_type='telegram_control'
                   AND signal='explicit' AND action='delegate'
                 ORDER BY occurred_at DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
        return row is not None
    raise RuntimeControlError(f"unknown runtime capability: {capability}")


def outbox_eligible(
    conn: sqlite3.Connection, config: Config, row: dict[str, Any]
) -> bool:
    from .notification_snooze import deferred

    if row["channel"] == "feishu_im" and row["action_type"] == "control_receipt":
        # A private control result must remain deliverable after its own
        # pause/stop action. Only a native message from the configured owner
        # in the configured control chat may create this exception.
        identity = config.raw["identity"]
        source = conn.execute(
            "SELECT sender_id,chat_id FROM inbound_events WHERE event_pk=?",
            (row.get("source_event_pk"),),
        ).fetchone()
        return bool(
            config.mode == "active"
            and identity.get("control_operator_id")
            and row["destination"] == identity.get("feishu_control_chat_id")
            and source is not None
            and source["sender_id"] == identity.get("feishu_control_user_id")
            and source["chat_id"] == row["destination"]
        )
    mode = str(current_global_state(conn, config)["mode"])
    if row.get("action_type") == "owner_digest":
        from .notification_snooze import status

        if status(conn,config=config)["active"]:
            return False
    if deferred(conn, row, config=config):
        return False
    if row["action_type"] == "p0_alert":
        return capability_allowed(conn, config, "p0_alert")
    if row["channel"] in {"telegram", "feishu_im"} and row["action_type"] in {
        "incident_alert",
        "mail_summary",
        "release_impact",
    }:
        # Observe suppresses colleague-facing automation, not the operator's
        # configured mail summaries or serious internal alerts.
        return config.mode == "active" and mode not in {"paused", "stopped"}
    if row["channel"] == "mail" and row["action_type"] in {
        "share_to_owner",
        "resolve_app_link",
    }:
        # These are private owner-facing steps of an enabled digest, not a
        # colleague-facing automated response. Observe may execute them.
        return (
            config.mode == "active"
            and config.feature("mail")
            and mode not in {"paused", "stopped"}
        )
    if row["channel"] == "feishu_im" and row["action_type"] == "send":
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            return False
        if payload.get("p0_owner_open_id"):
            return capability_allowed(conn, config, "p0_alert")
    if row["channel"] == "feishu_im" and row["action_type"] in PUBLIC_ACTIONS:
        return capability_allowed(
            conn, config, "public_reply", turn_id=row.get("turn_id")
        )
    if mode in {"paused", "stopped", "observe"}:
        return False
    return config.mode == "active"


def issue_global_panel(
    conn: sqlite3.Connection,
    config: Config,
    *,
    operator_user_id: str,
    chat_id: str,
    command_message_id: str,
    control_channel: str = "telegram",
) -> dict[str, Any]:
    if control_channel not in {"telegram", "gui", "feishu"}:
        raise RuntimeControlError("unknown control channel")
    with _tx(conn):
        state = ensure_global_state(
            conn,
            actor_id=config.control_operator_id,
            source=control_channel,
            external_id=f"{control_channel}-panel-init:{command_message_id}",
        )
        existing = conn.execute(
            "SELECT panel_id FROM global_control_panels WHERE command_message_id=?",
            (command_message_id,),
        ).fetchone()
        if existing is not None:
            return panel_payload(conn, config, panel_id=str(existing["panel_id"]))
        conn.execute(
            """UPDATE global_control_panels SET state='retired',updated_at=?
                 WHERE scope=? AND chat_id=? AND state='issued'""",
            (iso_now(), SCOPE, chat_id),
        )
        panel_id = new_id("gcp")
        now = iso_now()
        conn.execute(
            """INSERT INTO global_control_panels(panel_id,scope,operator_user_id,chat_id,
                   command_message_id,expected_global_revision,state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'issued',?,?)""",
            (
                panel_id,
                SCOPE,
                operator_user_id,
                chat_id,
                command_message_id,
                state["revision"],
                now,
                now,
            ),
        )
        conn.execute("UPDATE global_control_panels SET control_channel=? WHERE panel_id=?",
                     (control_channel, panel_id))
        return panel_payload(conn, config, panel_id=panel_id)


def bind_global_panel(
    conn: sqlite3.Connection,
    *,
    panel_id: str,
    operator_user_id: str,
    chat_id: str,
    command_message_id: str,
    prompt_message_id: str,
) -> dict[str, Any]:
    with _tx(conn):
        row = conn.execute(
            "SELECT * FROM global_control_panels WHERE panel_id=?", (panel_id,)
        ).fetchone()
        if row is None:
            raise RuntimeControlError("global control panel not found")
        if row["state"] == "retired":
            raise RuntimeControlError("global control panel is stale")
        if (
            row["operator_user_id"] != operator_user_id
            or row["chat_id"] != chat_id
            or row["command_message_id"] != command_message_id
        ):
            raise RuntimeControlError("global control panel identity mismatch")
        if row["prompt_message_id"] not in {None, prompt_message_id}:
            raise RuntimeControlError("global control panel is already bound")
        conn.execute(
            """UPDATE global_control_panels SET prompt_message_id=?,state='active',updated_at=?
                 WHERE panel_id=?""",
            (prompt_message_id, iso_now(), panel_id),
        )
        conn.execute(
            """UPDATE global_control_panels SET state='retired',updated_at=?
                 WHERE scope=? AND chat_id=? AND panel_id<>? AND state<>'retired'""",
            (iso_now(), SCOPE, chat_id, panel_id),
        )
    return {"panel_id": panel_id, "prompt_message_id": prompt_message_id, "bound": True}


def _panel_buttons(panel: sqlite3.Row, *, mode: str) -> list[dict[str, Any]]:
    panel_id = str(panel["panel_id"])
    pending = panel["pending_confirmation"]
    if pending == "auto":
        return [
            {"text": "确认进入自动", "callback_data": f"fsc:A:{panel_id}", "row": 0},
            {"text": "取消", "callback_data": f"fsc:x:{panel_id}", "row": 0},
            {"text": "详情", "callback_data": f"fsc:i:{panel_id}", "row": 1},
        ]
    if pending == "stopped":
        return [
            {"text": "确认完全停止", "callback_data": f"fsc:S:{panel_id}", "row": 0},
            {"text": "取消", "callback_data": f"fsc:x:{panel_id}", "row": 0},
            {"text": "详情", "callback_data": f"fsc:i:{panel_id}", "row": 1},
        ]
    def active(value: str, text: str) -> str:
        return f"✅ {text}" if mode == value else text

    return [
        {"text": active("observe", "观察"), "callback_data": f"fsc:o:{panel_id}", "row": 0},
        {"text": active("collaborate", "协作"), "callback_data": f"fsc:c:{panel_id}", "row": 0},
        {"text": active("auto_60", "自动60分钟"), "callback_data": f"fsc:t:{panel_id}", "row": 1},
        {"text": active("auto", "自动"), "callback_data": f"fsc:a:{panel_id}", "row": 1},
        {"text": active("paused", "立即暂停"), "callback_data": f"fsc:p:{panel_id}", "row": 2},
        {"text": active("stopped", "完全停止"), "callback_data": f"fsc:s:{panel_id}", "row": 2},
        {"text": "详情", "callback_data": f"fsc:i:{panel_id}", "row": 3},
        {"text": "刷新", "callback_data": f"fsc:r:{panel_id}", "row": 3},
        {"text": "待处理工作台", "callback_data": f"fsc:w:{panel_id}", "row": 4},
    ]


def panel_payload(
    conn: sqlite3.Connection, config: Config, *, panel_id: str, detailed: bool = False
) -> dict[str, Any]:
    from .workbench import workbench_counts

    state = current_global_state(conn, config)
    panel = conn.execute(
        "SELECT * FROM global_control_panels WHERE panel_id=?", (panel_id,)
    ).fetchone()
    if panel is None:
        raise RuntimeControlError("global control panel not found")
    queue_counts = workbench_counts(conn, config=config)
    case_counts = {
        str(row["state"]): int(row["count"])
        for row in conn.execute(
            """SELECT state,count(*) AS count FROM cases
                 WHERE state NOT IN ('resolved','cancelled','takeover') GROUP BY state"""
        )
    }
    pending_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM outbox WHERE state IN ('pending','retry','sending')"
        )
    ]
    sendable = sum(outbox_eligible(conn, config, row) for row in pending_rows)
    held = len(pending_rows) - sendable
    board = conn.execute(
        "SELECT case_id,expires_at FROM locks WHERE lock_key='board1'"
    ).fetchone()
    remaining = "-"
    if state.get("auto_expires_at"):
        seconds = max(
            0,
            int(
                (
                    parse_iso(state["auto_expires_at"]) - utc_now()
                ).total_seconds()
            ),
        )
        remaining = f"{seconds // 60} 分 {seconds % 60} 秒"
    enabled_features = [
        label
        for feature, label in (
            ("auto_faq", "知识回复"),
            ("codex", "Codex"),
            ("board", "board1"),
            ("wip_push", "WIP push"),
            ("mail", "邮箱"),
            ("calendar", "会议"),
            ("base_sync", "Base"),
        )
        if config.feature(feature)
    ]
    lines = [
        "飞书自动办公总控",
        f"当前模式：{MODE_LABELS[str(state['mode'])]}",
        f"生效时间：{state.get('changed_at') or '-'}",
        f"自动剩余：{remaining}",
        f"待你判断：{queue_counts['owner_decision']}",
        f"AI 处理中：{queue_counts['ai_processing']}",
        f"人工负责：{queue_counts['human_hold']}｜等待：{queue_counts['waiting']}",
        f"资料咨询已答：{queue_counts['answered_faq']}（不占待办）",
        f"观察记录：{case_counts.get('intake', 0) + case_counts.get('triage', 0)}",
        f"待发送：{sendable}" + (f"（暂挂 {held}）" if held else ""),
        f"board1：{('无控制面租约，实际占用未知' if board is None else '待清理/占用 ' + str(board['case_id']))}",
        f"已启用：{'、'.join(enabled_features) or '无'}",
    ]
    if config.feature("auto_faq"):
        from .knowledge_release import verify_release

        knowledge_release = verify_release(conn, config)
        lines.append(
            "知识自动回复：" + ("发布签名已验证，实际模型仍须核验" if knowledge_release.get("artifact_verified")
                              else "发布门未通过（打开自动模式不会绕过）")
        )
    if config.mode != "active":
        lines.append(f"静态闸门：{config.mode}（执行与外发仍关闭）")
    if panel["pending_confirmation"]:
        label = MODE_LABELS[str(panel["pending_confirmation"])]
        lines.extend(("", f"请再次确认：{label}（60 秒内有效）"))
    if detailed:
        from .workbench import render_workbench, workbench_snapshot

        jobs = conn.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('queued','running','waiting')"
        ).fetchone()[0]
        lines.extend(
            (
                "",
                f"全局版本：{state['revision']}",
                f"活动任务：{jobs}",
                "",
                render_workbench(workbench_snapshot(conn, config=config, limit=8)),
            )
        )
    return {
        "command": "global_panel",
        "panel_id": panel_id,
        "mode": state["mode"],
        "revision": state["revision"],
        "text": "\n".join(lines),
        "buttons": _panel_buttons(panel, mode=str(state["mode"])),
    }


def execute_global_callback(
    conn: sqlite3.Connection,
    config: Config,
    *,
    action: str,
    panel_id: str,
    operator_user_id: str,
    chat_id: str,
    callback_query_id: str,
    prompt_message_id: str,
) -> dict[str, Any]:
    action_modes = {
        "global_observe": "observe",
        "global_collaborate": "collaborate",
        "global_auto_60": "auto_60",
        "global_pause": "paused",
    }
    with _tx(conn):
        panel = conn.execute(
            "SELECT * FROM global_control_panels WHERE panel_id=?", (panel_id,)
        ).fetchone()
        if panel is None or panel["state"] != "active":
            raise RuntimeControlError("global control panel is stale")
        if (
            panel["operator_user_id"] != operator_user_id
            or panel["chat_id"] != chat_id
            or str(panel["prompt_message_id"] or "") != str(prompt_message_id)
        ):
            raise RuntimeControlError("global control callback identity mismatch")
        if action == "global_workbench":
            # Navigation must not expire/change a mode or confirmation. Read
            # stored state directly instead of calling current_global_state.
            state_row = conn.execute("SELECT revision FROM global_control_state WHERE scope=?", (SCOPE,)).fetchone()
            if state_row is None or int(panel["expected_global_revision"]) != int(state_row["revision"]):
                raise RuntimeControlError("global control panel is stale")
            from .workbench import workbench_panel

            return workbench_panel(conn, config)
        state = current_global_state(conn, config)
        if int(panel["expected_global_revision"]) != int(state["revision"]):
            raise RuntimeControlError("global control panel is stale")
        now = utc_now()
        if (
            panel["confirmation_expires_at"]
            and parse_iso(panel["confirmation_expires_at"]) <= now
        ):
            conn.execute(
                """UPDATE global_control_panels SET pending_confirmation=NULL,
                       confirmation_expires_at=NULL,updated_at=? WHERE panel_id=?""",
                (now.isoformat(), panel_id),
            )
            panel = conn.execute(
                "SELECT * FROM global_control_panels WHERE panel_id=?", (panel_id,)
            ).fetchone()
        if action in {"global_auto_request", "global_stop_request"}:
            pending = "auto" if action == "global_auto_request" else "stopped"
            conn.execute(
                """UPDATE global_control_panels SET pending_confirmation=?,
                       confirmation_expires_at=?,updated_at=? WHERE panel_id=?""",
                (
                    pending,
                    (now + timedelta(seconds=CONFIRM_SECONDS)).isoformat(),
                    now.isoformat(),
                    panel_id,
                ),
            )
            return panel_payload(conn, config, panel_id=panel_id)
        if action == "global_cancel_confirmation":
            conn.execute(
                """UPDATE global_control_panels SET pending_confirmation=NULL,
                       confirmation_expires_at=NULL,updated_at=? WHERE panel_id=?""",
                (now.isoformat(), panel_id),
            )
            return panel_payload(conn, config, panel_id=panel_id)
        if action in {"global_details", "global_refresh"}:
            return panel_payload(
                conn, config, panel_id=panel_id, detailed=action == "global_details"
            )
        if action == "global_auto_confirm":
            if panel["pending_confirmation"] != "auto":
                raise RuntimeControlError("自动模式缺少二次确认")
            after = "auto"
        elif action == "global_stop_confirm":
            if panel["pending_confirmation"] != "stopped":
                raise RuntimeControlError("完全停止缺少二次确认")
            after = "stopped"
        elif action in action_modes:
            after = action_modes[action]
        else:
            raise RuntimeControlError("unsupported global control action")
        changed = _transition(
            conn,
            after=after,
            actor_id=config.control_operator_id,
            source=f"{panel['control_channel']}_button",
            external_id=f"{panel['control_channel']}-callback:{callback_query_id}",
            reason=f"operator selected {after}",
            expected_revision=int(state["revision"]),
            now=now,
        )
        conn.execute(
            """UPDATE global_control_panels SET expected_global_revision=?,
                   pending_confirmation=NULL,confirmation_expires_at=NULL,updated_at=?
                 WHERE panel_id=?""",
            (changed["revision"], now.isoformat(), panel_id),
        )
        conn.execute(
            """UPDATE global_control_panels SET state='retired',updated_at=?
                 WHERE scope=? AND panel_id<>? AND state<>'retired'""",
            (now.isoformat(), SCOPE, panel_id),
        )
        return panel_payload(conn, config, panel_id=panel_id)
