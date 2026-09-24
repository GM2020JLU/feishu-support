"""Exact, phone-readable meeting review over a delivered Telegram prompt."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime

from .approvals import ApprovalError, verify_control_identity
from .calendar import (
    CalendarError,
    exact_meeting_preview,
    historical_meeting_preview,
    queue_meeting_preview,
)
from .db import transaction
from .knowledge_preview import _pages
from .timeutil import iso_now, parse_iso


def _button(preview, operation, coordinate, label):
    argument = f"{coordinate}." if coordinate is not None else ""
    data = f"mt:{operation}:{preview['preview_id'][4:]}:{argument}{preview['action_digest'][:16]}"
    if len(data.encode()) > 64:
        raise CalendarError("meeting callback too long")
    return {"text": label, "callback_data": data}


def _context(conn, config, message, prompt_id, preview_id):
    verify_control_identity(config, message.user_id, message.chat_id)
    preview, action = historical_meeting_preview(conn, preview_id)
    bound = False
    for row in conn.execute(
        """SELECT payload_json,remote_message_id,destination FROM outbox WHERE channel='telegram'
           AND action_type='approval_request' AND state='delivered' AND case_id=?""",
        (preview["case_id"],),
    ):
        payload = json.loads(row["payload_json"])
        if (
            str(row["remote_message_id"]) == prompt_id
            and row["destination"] == f"telegram:{message.chat_id}"
            and payload.get("preview_id") == preview_id
            and payload.get("approval_id") == preview["approval_id"]
            and payload.get("meeting_action_digest") == preview["action_digest"]
            and _button(preview, "v", 1, "查看完整预览") in payload.get("buttons", [])
        ):
            bound = True
            break
    if not bound:
        raise ApprovalError(
            "meeting callback is not bound to the exact delivered preview"
        )
    return preview, action


def _render(preview, action, page):
    availability = action.get("availability") or {}
    state = availability.get("state", "unknown")
    labels = {
        "available": "所查询范围未发现冲突（不是占位保证）",
        "conflict": "已发现冲突",
        "unknown": "未知：没有完整忙闲证据",
    }
    lines = [
        f"主题：{action['summary']}",
        f"时区：{action['timezone']}",
        f"时间：{action['start']} → {action['end']}",
        "执行身份：你的飞书用户身份；确认后会创建日程并邀请以下对象。",
        "精确参会对象（不猜姓名或展开群成员）：",
        *[f"- {value}" for value in action["attendee_ids"]],
        f"重复规则：{action.get('rrule') or '不重复'}",
        f"议程全文：\n{action['description'] or '未填写'}",
        "\n忙闲检查：" + labels.get(state, labels["unknown"]),
        f"查询时间：{availability.get('queried_at') or '未查询'}",
        f"查询覆盖：{availability.get('covered_start') or '未知'} → {availability.get('covered_end') or '未知'}",
        "已查询：" + ", ".join(availability.get("checked_ids") or []),
        "未确认："
        + ", ".join(
            availability.get("unknown_ids")
            or ([] if state != "unknown" else ["未接忙闲查询"])
        ),
    ]
    lines.extend(
        f"冲突：{value['attendee_id']} · {value['start']} → {value['end']}"
        for value in availability.get("conflicts", [])
    )
    if action.get("schema_version") == 2:
        lines.extend(
            [
                "原日历和身份："
                + json.dumps(action["target"], ensure_ascii=False, sort_keys=True),
                "完整创建请求（议程按 API 文本/HTML 处理，无文件上传）：\n"
                + json.dumps(action["create_body"], ensure_ascii=False, sort_keys=True),
                "完整邀请请求：\n"
                + json.dumps(
                    action["attendees_body"], ensure_ascii=False, sort_keys=True
                ),
                "失败策略：已创建的日程保留；邀请结果未知不自动重试，也不自动删除。会议室预约结果另行显示。",
                "操作标识：" + action["operation_id"],
            ]
        )
    else:
        lines.append(
            "历史 v1 预览：没有固定原日历和完整副作用，不能执行；请重新生成完整预览。"
        )
    alternatives = availability.get("alternatives", [])
    lines.extend(
        f"候选 {index}：{value['start']} → {value['end']}（仅已查询对象/范围，选择后重新预览审批）"
        for index, value in enumerate(alternatives, 1)
    )
    lines.extend(
        [
            "忙闲是当时查询结果，不保证所有日历或临近创建时仍无变化。",
            "日程含视频会议；5分钟提醒仅对你的日历生效，参会人可互见并编辑日程。",
            f"审批状态：{preview['approval_status']}；有效期：{preview['expires_at']}",
            f"精确版本：{preview['action_digest']}",
        ]
    )
    pages = _pages("\n".join(lines))
    if not 1 <= page <= len(pages):
        raise CalendarError("meeting preview page is out of range")
    buttons = []
    for target, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= len(pages):
            buttons.append(_button(preview, "v", target, label))
    active = (
        action.get("schema_version") == 2
        and preview["approval_round"] == preview["current_round"]
        and preview["case_state"] not in {"resolved", "cancelled", "paused", "takeover"}
        and parse_iso(action["start"]) > datetime.now(UTC)
        and preview["approval_status"] == "requested"
        and parse_iso(preview["expires_at"]) > datetime.now(UTC)
    )
    if page == len(pages) and active:
        label = {"conflict": "知晓冲突，仍创建", "unknown": "知晓忙闲未知，创建"}.get(
            state, "确认创建此会议"
        )
        buttons.append(_button(preview, "a", None, label))
        buttons.extend(
            _button(preview, "s", index, f"选择候选 {index}")
            for index in range(1, len(alternatives) + 1)
        )
    if active:
        buttons.append(
            {"text": "不创建", "callback_data": f"k3a:d:{preview['approval_id']}"}
        )
    buttons.append(_button(preview, "r", None, "查看创建状态 / 恢复"))
    return {
        "command": "meeting_preview",
        "operation": "show",
        "preview": {
            "text": f"<b>会议完整预览 · {page}/{len(pages)} 页</b>\n\n"
            + pages[page - 1],
            "parse_mode": "HTML",
            "plain_text": html.unescape(pages[page - 1]),
            "page": page,
            "page_count": len(pages),
            "buttons": buttons,
        },
    }


def route(conn, config, message, *, prompt_message_id, callback_data):
    match = re.fullmatch(
        r"mt:([vsar]):([0-9a-f]{32}):(?:(\d{1,5})\.)?([0-9a-f]{16})", callback_data
    )
    if match is None or len(callback_data.encode()) > 64:
        raise CalendarError("invalid meeting preview callback")
    operation, identifier, coordinate, fingerprint = match.groups()
    preview, action = _context(
        conn, config, message, prompt_message_id, "mtg_" + identifier
    )
    if fingerprint != preview["action_digest"][:16]:
        raise ApprovalError("meeting exact action changed; generate a new preview")
    if operation == "v":
        return _render(preview, action, int(coordinate or 0))
    if operation == "r":
        if coordinate is not None:
            raise CalendarError("invalid meeting recovery navigation")
        from .meeting_recovery import meeting_recovery_report

        return recovery_panel(
            meeting_recovery_report(conn, config, preview_id=preview["preview_id"])
        )
    exact_meeting_preview(conn, preview["preview_id"])
    if action.get("schema_version") != 2:
        raise ApprovalError("legacy meeting approval requires a new exact preview")
    if preview["approval_status"] != "requested" or parse_iso(
        preview["expires_at"]
    ) <= datetime.now(UTC):
        raise ApprovalError("meeting approval is no longer pending")
    if operation == "a":
        if coordinate is not None:
            raise CalendarError("invalid meeting confirmation")
        from .control import _decide_and_continue

        return _decide_and_continue(
            conn,
            config,
            message,
            approval_id=preview["approval_id"],
            approve=True,
            expected_digest=preview["action_digest"],
        )
    alternatives = (action.get("availability") or {}).get("alternatives", [])
    if coordinate is None or not 1 <= int(coordinate) <= len(alternatives):
        raise CalendarError("invalid meeting alternative")
    option = alternatives[int(coordinate) - 1]
    new_action = {
        **action,
        "start": option["start"],
        "end": option["end"],
        "availability": {
            **action["availability"],
            "state": "available",
            "conflicts": [],
            "alternatives": [],
        },
    }
    from .meeting_recovery import revise_bound_action

    new_action = revise_bound_action(new_action)
    # Revoke before creating the successor. A crash may require a new preview,
    # but must never leave old and new meeting times authorized together.
    with transaction(conn):
        exact_meeting_preview(conn, preview["preview_id"])
        changed = conn.execute(
            "UPDATE approvals SET status='revoked',updated_at=? WHERE approval_id=? AND status='requested'",
            (iso_now(), preview["approval_id"]),
        )
        if changed.rowcount != 1:
            raise ApprovalError("meeting approval is no longer pending")
        conn.execute(
            "UPDATE meeting_previews SET status='expired',updated_at=? WHERE preview_id=?",
            (iso_now(), preview["preview_id"]),
        )
    successor = queue_meeting_preview(conn, config, action=new_action)
    return {
        "command": "meeting_preview",
        "operation": "alternative_queued",
        "successor": successor,
        "preview": {
            "text": "<b>新时段预览待送达</b>\n原时段审批已撤销；请在新卡查看完整预览并确认。\n没有创建会议或发送邀请。",
            "parse_mode": "HTML",
            "page": 1,
            "page_count": 1,
            "buttons": [],
        },
    }


def recovery_panel(report, *, page=1):
    """Pure owner-card projection. The shared controller authenticates callbacks.

    mr:v/c/x/n use preview UUID plus attempt revision (and a page for v).
    mr:b/a use observation UUID plus digest prefix. No event/target is accepted
    from a button; the controller resolves the immutable stored observation.
    """
    preview_id = report["preview_id"]
    if not re.fullmatch(r"mtg_[0-9a-f]{32}", preview_id):
        raise CalendarError("invalid recovery preview ID")
    revision = report.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise CalendarError("invalid recovery revision")
    phase = report["phase"]
    labels = {
        "prepared": "尚未进入创建发送，可原子取消",
        "dispatched": "创建结果待确认",
        "uncertain": "创建结果未知，禁止重试",
        "legacy_uncertain": "历史创建结果未知，原日历未记录",
        "event_created": "日程已创建，邀请尚未完成",
        "inviting": "日程已创建，邀请结果待确认",
        "partial": "日程已创建，邀请未完整核实",
        "complete": "日程及邀请对象已确认，会议室结果单列",
        "linked": "已核实原回执对应日程",
        "adopted": "已人工采用现有日程；原调用结果仍未知",
        "never_dispatched": "有原子证据证明未发送创建",
        "conflict": "发现冲突回执，需人工处理",
        "not_attempted": "尚未执行创建",
        "legacy_created": "旧系统记录日程已创建；原日历与新回执账本未核验",
    }
    action = report["action"]
    lines = [
        labels.get(phase, "未知状态"),
        f"主题：{action['summary']}",
        f"时间：{action['start']} → {action['end']}",
        f"时区：{action['timezone']}",
        f"Case：{action['case_id']}",
        f"关联日程：{report.get('event_id') or '未确认'}",
        "核对只读取远端；不会创建、更新、邀请或删除。",
    ]
    if report.get("original_outcome_uncertain") or phase == "conflict":
        lines.append("已关联不等于原创建成功；仍禁止重复创建或用新审批绕过未知结果。")
    if phase == "legacy_uncertain" and not report.get("adopted_target"):
        lines.append(
            "旧记录缺少原日历。需本人明确指定搜索日历；不会自动扫描今天的主日历。"
        )
    observations = report.get("observations", [])
    for item in observations:
        lines.append(
            f"\n{item['kind']} · {item['created_at']}\n"
            + json.dumps(item["payload"], ensure_ascii=False, sort_keys=True)
        )
    total = report.get("observation_total", len(observations))
    lines.append(f"历史证据显示最近 {len(observations)} / {total} 条；旧证据未删除。")
    pages = _pages("\n".join(lines))
    if type(page) is not int or not 1 <= page <= len(pages):
        raise CalendarError("recovery page is out of range")
    buttons = []

    def add(operation, value, label):
        callback = f"mr:{operation}:{preview_id[4:]}:{value}"
        if len(callback.encode()) > 64:
            raise CalendarError("recovery callback too long")
        buttons.append({"text": label, "callback_data": callback})

    for target, label in ((page - 1, "上一页"), (page + 1, "下一页")):
        if 1 <= target <= len(pages):
            add("v", f"{target}.{revision}", label)
    if phase != "not_attempted":
        add("c", str(revision), "核对结果（只读）")
    if page == len(pages):
        if phase == "not_attempted" and report.get("requires_new_v2_preview"):
            callback = f"mr:u:{preview_id[4:]}:{report['action_digest'][:16]}"
            buttons.append({"text": "重新生成完整审批", "callback_data": callback})
        if report.get("can_cancel_before_dispatch"):
            add("x", str(revision), "取消尚未发送的创建")
        if report.get("can_repreview"):
            add("n", str(revision), "重新生成完整审批")
        latest = next((item for item in observations if item["kind"] == "check"), None)
        if (
            latest
            and phase not in {"conflict", "never_dispatched", "linked", "adopted"}
            and latest["payload"].get("attempt_revision") == revision
        ):
            classification = latest["payload"].get("classification")
            operation = (
                "b"
                if classification == "known_created"
                else "a"
                if classification in {"unique_candidate", "legacy_candidate"}
                else None
            )
            if operation:
                callback = f"mr:{operation}:{latest['observation_id'][4:]}:{latest['digest'][:16]}"
                if len(callback.encode()) > 64:
                    raise CalendarError("recovery callback too long")
                buttons.append(
                    {
                        "text": "核实绑定原日程"
                        if operation == "b"
                        else "采用已有日程（原结果仍未知）",
                        "callback_data": callback,
                    }
                )
    return {
        "command": "meeting_recovery",
        "operation": "show",
        "preview": {
            "text": f"<b>会议恢复 · {page}/{len(pages)} 页</b>\n\n" + pages[page - 1],
            "parse_mode": "HTML",
            "page": page,
            "page_count": len(pages),
            "buttons": buttons + [{"text": "刷新状态", "callback_data":
                f"mt:r:{preview_id[4:]}:{report['action_digest'][:16]}"}],
        },
    }
