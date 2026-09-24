"""Authenticated chat controls for existing, prepared Project Bug writes.

This module only reads local operation state and delegates mutations to the
existing Bug control queues. It never obtains credentials or calls Project.
"""

import json

from . import project_bug_operations as operations
from .db import transaction
from .ids import canonical_json, digest
from .timeutil import iso_now

COMMANDS = {"writes", "write", "send-write", "cancel-write", "reconcile-write"}
_MAX_PREVIEW_UNITS = 3000


def _intent_conflict():
    from .project_bugs import BugConflict
    raise BugConflict("native message ID is already bound to a different write intent")


def _owned(conn, actor, operation_id):
    op = operations._operation(conn, operation_id)
    if op["actor"] != actor:
        raise ValueError("owned Bug write operation required")
    return op


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _preview(conn, op):
    from . import project_bugs

    bug = conn.execute(
        "SELECT host,project_key,type_key,item_id,bug_id FROM project_bugs WHERE bug_id=?",
        (op["bug_id"],),
    ).fetchone()
    if bug is None:
        raise ValueError("Bug write target is unavailable")
    change = json.loads(op["change_json"])
    current = project_bugs.detail(conn, op["bug_id"])["snapshot"]
    if current is None:
        current = operations._snapshot(conn, op["bug_id"], op["snapshot_id"])
    computed = operations.preview(conn, op["operation_id"], current)
    intent = {
        "target": {key: bug[key] for key in ("host", "project_key", "type_key", "item_id", "bug_id")},
        "action": op["action"],
        "state": op["state"],
        "proposed_change": change,
        "cached_comparison": computed,
    }
    return intent


def _json(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # JSON escapes ASCII line breaks; keep Unicode line separators literal data
    # too, while preserving readable Chinese text in full previews.
    for separator in ("\u0085", "\u2028", "\u2029"):
        text = text.replace(separator, "\\u" + format(ord(separator), "04x"))
    return text


def _reserve_intent(conn, *, actor, bug_id, request_id, argv):
    """Bind one native message ID to one exact local write-control intent."""
    event_id = "pbe_chat_" + digest({"actor": actor, "request_id": request_id})
    detail = {"request_id": request_id, "intent_digest": digest(argv)}
    with transaction(conn):
        row = conn.execute(
            "SELECT bug_id,actor,kind,detail_json FROM project_bug_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is not None:
            if (row["bug_id"] != bug_id or row["actor"] != actor
                    or row["kind"] != "chat_write_intent"
                    or json.loads(row["detail_json"]) != detail):
                _intent_conflict()
            return
        conn.execute(
            "INSERT INTO project_bug_events(event_id,bug_id,round_id,actor,kind,detail_json,created_at) "
            "VALUES(?,?,NULL,?,?,?,?)",
            (event_id, bug_id, actor, "chat_write_intent", canonical_json(detail), iso_now()),
        )


def reserve_intent(conn, config, *, bug_id, argv, request_id):
    """Public hook for sibling authenticated Bug chat routes."""
    actor = config.control_operator_id
    if not actor:
        raise PermissionError("Bug controls require a configured operator")
    _reserve_intent(conn, actor=actor, bug_id=bug_id, request_id=request_id, argv=argv)


def check_intent(conn, config, argv, request_id):
    """Reject an edited command reusing a message already bound to a write."""
    actor = config.control_operator_id
    if not actor:
        raise PermissionError("Bug controls require a configured operator")
    event_id = "pbe_chat_" + digest({"actor": actor, "request_id": request_id})
    row = conn.execute(
        "SELECT actor,kind,detail_json FROM project_bug_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        return False
    detail = json.loads(row["detail_json"])
    if (row["actor"] != actor or row["kind"] != "chat_write_intent"
            or detail != {"request_id": request_id, "intent_digest": digest(argv)}):
        _intent_conflict()
    return True


def _reserve_for_operation(conn, actor, request_id, argv, op):
    _reserve_intent(conn, actor=actor, bug_id=op["bug_id"], request_id=request_id, argv=argv)


def _dispatch_action(action):
    return "send-comment" if action == "bug.comment" else "send-write"


def _dispatch_controls(conn, config, actor, op):
    if op["action"] == "bug.comment":
        from .project_comment_dispatch import controls
    else:
        from .project_write_dispatch import controls
    value = controls(conn, config, actor=actor, operation_id=op["operation_id"])
    if value["available"] and op["action"] == "bug.close" and not _close_approval_available(conn, config, op):
        value.update(available=False, reason="current_close_approval_or_verification_required")
    return value


def _preview_text(op, intent, send_control, reconciliation=None):
    target = intent["target"]
    change = intent["proposed_change"]
    comparison = intent["cached_comparison"]
    lines = [
        "完整写入预览（以下比较使用本地缓存，不代表远端实时状态）",
        f"操作：{op['operation_id']} · {intent['action']} · {intent['state']}",
        "目标：" + " / ".join(_json(target[key]) for key in ("host", "project_key", "type_key", "item_id")),
        f"Bug：{target['bug_id']}",
    ]
    if intent["action"] == "bug.comment":
        lines.append("完整评论文本（JSON 字符串编码）：" + _json(change["text"]))
    elif intent["action"] == "bug.fields":
        differences = {item["field"]: item for item in comparison["differences"]}
        lines.append("字段变更（原值 / 当前缓存 / 拟写入值）：")
        for field, wanted in sorted(change["fields"].items()):
            item = differences.get(field, {})
            base = _json(item.get("base")) if item.get("base_present") else "未观测"
            current = _json(item.get("current")) if item.get("current_present") else "未观测"
            lines.append(f"{_json(field)} · {item.get('state', '不可比较')}\n原值：{base}\n当前缓存：{current}\n拟写入：{_json(wanted)}")
    else:
        lines.extend([
            f"拟执行的状态流转 ID：{_json(change['transition_id'])}",
            f"目标状态 ID：{_json(change['target_status_id'])}",
            f"状态缓存是否变化：{'是' if comparison['status_changed'] else '否'}",
            f"字段结构缓存是否变化：{'是' if comparison['schema_changed'] else '否'}",
        ])
    requests = send_control.get("requests", [])
    pending = any(item["state"] in {"queued", "running"} for item in requests)
    if requests:
        lines.append("现有发送队列：")
        for item in requests[:3]:
            extra = f" · 错误：{item['error_code']}" if item.get("error_code") else ""
            result = item.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            if result.get("operation_state"):
                extra += f" · 操作结果：{result['operation_state']}"
            if result.get("reconcile_required"):
                extra += " · 需要只读核对"
            lines.append(f"{item['dispatch_id']} · {item['state']} · 尝试 {item['attempt']}{extra}")
    if op["state"] == "prepared":
        if pending:
            lines.append("已有队列正在处理；不会提供第二条发送命令。")
        elif send_control.get("available"):
            lines.append(f"队列发送：bug send-write {op['operation_id']} {op['request_digest']}")
            lines.append(f"取消本地准备：bug cancel-write {op['operation_id']} {op['request_digest']}")
        else:
            lines.append("当前不能排队发送：" + str(send_control.get("reason") or "existing_dispatch_gate_blocked"))
            if not pending:
                lines.append(f"取消本地准备：bug cancel-write {op['operation_id']} {op['request_digest']}")
    elif op["state"] in {"dispatched", "unknown"}:
        if op["write_digest"]:
            lines.append(f"只读核对：bug reconcile-write {op['operation_id']} {op['write_digest']} <只读授权ID>")
        lines.append("结果待核对时不会重发；使用只读核对命令。")
    if reconciliation:
        lines.append("最近只读核对队列：")
        for item in reconciliation[:3]:
            extra = f" · 错误：{item['error_code']}" if item.get("error_code") else ""
            lines.append(f"{item['activity_id']} · {item['state']}{extra} · 核对不会重发写入")
    return "\n".join(lines)


def _reconciliation_status(conn, actor, operation_id):
    rows = conn.execute(
        "SELECT activity_id,state,error_code FROM project_activity_requests "
        "WHERE actor=? AND kind IN ('comment_reconcile','write_reconcile') "
        "AND json_valid(source_json) AND json_extract(source_json,'$.operation_id')=? "
        "ORDER BY rowid DESC LIMIT 3",
        (actor, operation_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _existing_dispatch(conn, actor, op, request_id):
    from .ids import digest as make_digest
    table = "project_comment_dispatch_requests" if op["action"] == "bug.comment" else "project_write_dispatch_requests"
    row = conn.execute(
        f"SELECT dispatch_id,operation_id,request_digest FROM {table} WHERE actor=? AND request_id=?",
        (actor, request_id),
    ).fetchone()
    if row is None:
        return None
    expected = make_digest({"operation_id": op["operation_id"], "expected_digest": op["request_digest"]})
    if row["operation_id"] != op["operation_id"] or row["request_digest"] != expected:
        _intent_conflict()
    from . import project_comment_dispatch, project_write_dispatch
    status = project_comment_dispatch.status if op["action"] == "bug.comment" else project_write_dispatch.status
    return status(conn, actor=actor, dispatch_id=row["dispatch_id"])


def _close_approval_available(conn, config, op):
    from . import project_close_gate
    bug = conn.execute("SELECT * FROM project_bugs WHERE bug_id=?", (op["bug_id"],)).fetchone()
    change = json.loads(op["change_json"])
    expected = digest(project_close_gate.action(bug, change))
    latest = conn.execute(
        "SELECT approval_id FROM project_close_approvals WHERE bug_id=? AND status='approved' ORDER BY rowid DESC LIMIT 1",
        (op["bug_id"],),
    ).fetchone()
    if latest is None:
        return False
    approval = project_close_gate.detail(conn, latest["approval_id"], config=config)
    current = approval.get("current_verification")
    return bool(
        approval["status"] == "approved" and not approval["expired"]
        and approval["action_digest"] == expected
        and approval["action"] == project_close_gate.action(bug, change)
        and current and current.get("verification_state") == "passed"
        and current.get("digest") == approval["verification_digest"]
    )


def route(conn, config, argv, request_id):
    """Route ``bug <write-command>`` after upstream native identity validation."""
    from .control import ControlError
    from .project_bug_controls import execute

    actor = config.control_operator_id
    if not actor:
        raise PermissionError("Bug controls require a configured operator")
    if len(argv) == 3 and argv[1] == "writes":
        bug_id = argv[2]
        rows = conn.execute(
            "SELECT operation_id,action,state,request_digest,write_digest,created_at "
            "FROM project_bug_operations WHERE actor=? AND bug_id=? ORDER BY rowid DESC LIMIT 9",
            (actor, bug_id),
        ).fetchall()
        lines = [f"本地写入记录 · Bug {bug_id}"]
        if not rows:
            lines.append("没有当前操作人的写入记录。")
        for row in rows[:8]:
            lines.append(f"{row['operation_id']} · {row['action']} · {row['state']}\n查看完整预览：bug write {row['operation_id']}")
        if len(rows) > 8:
            lines.append("仅显示最新 8 条；更多记录请在网页查看。")
        return {"command": "project_bug", "text": "\n\n".join(lines)}

    if len(argv) == 3 and argv[1] == "write":
        op = _owned(conn, actor, argv[2])
        intent = _preview(conn, op)
        controls = _dispatch_controls(conn, config, actor, op)
        reconciliations = _reconciliation_status(conn, actor, op["operation_id"])
        text = _preview_text(op, intent, controls, reconciliations)
        if _units(text) > _MAX_PREVIEW_UNITS:
            return {"command": "project_bug", "text":
                    "完整写入预览超过聊天显示上限，未提供可执行命令。请在网页查看完整预览；不得依据截断内容发送。"}
        return {"command": "project_bug", "text": text}

    if len(argv) == 4 and argv[1] == "send-write":
        op = _owned(conn, actor, argv[2])
        already_bound = check_intent(conn, config, argv, request_id)
        if argv[3] != op["request_digest"]:
            raise ControlError("写入摘要不匹配；请重新查看完整预览。")
        existing = _existing_dispatch(conn, actor, op, request_id)
        if existing is not None:
            if not already_bound:
                _reserve_for_operation(conn, actor, request_id, argv, op)
            return {"command": "project_bug", "text":
                    f"已有写入队列回执：{existing['dispatch_id']} · {existing['state']}。未创建新请求。"}
        if op["state"] != "prepared":
            raise ControlError("操作已派发、待核对或结束；不会重复发送，请使用只读核对。")
        send_control = _dispatch_controls(conn, config, actor, op)
        text = _preview_text(op, _preview(conn, op), send_control, _reconciliation_status(conn, actor, op["operation_id"]))
        if _units(text) > _MAX_PREVIEW_UNITS:
            raise ControlError("完整写入预览超过聊天显示上限，请在网页查看；聊天命令不会发送。")
        if not send_control.get("available"):
            raise ControlError("当前发送队列不可用：" + str(send_control.get("reason") or "existing_dispatch_gate_blocked"))
        if any(item["state"] in {"queued", "running"} for item in send_control.get("requests", [])):
            raise ControlError("已有发送队列正在处理；不会创建第二条发送请求。")
        if op["action"] == "bug.close" and not _close_approval_available(conn, config, op):
            raise ControlError("关闭操作缺少当前有效的原有关闭审批或验证证据，不能发送。")
        if not already_bound:
            _reserve_for_operation(conn, actor, request_id, argv, op)
        result = execute(conn, config, action=_dispatch_action(op["action"]), payload={
            "operation_id": op["operation_id"], "expected_digest": op["request_digest"],
            "request_id": request_id,
        })
        return {"command": "project_bug", "text":
                f"已进入现有写入队列：{result['dispatch_id']} · {result['state']}。"
                "此命令只排队，不直接访问远端；查询完整预览：bug write " + op["operation_id"]}

    if len(argv) == 4 and argv[1] == "cancel-write":
        op = _owned(conn, actor, argv[2])
        check_intent(conn, config, argv, request_id)
        if argv[3] != op["request_digest"]:
            raise ControlError("写入摘要不匹配。")
        if op["state"] not in {"prepared", "cancelled"}:
            raise ControlError("仅可取消仍处于本地准备状态的写入。")
        if op["state"] == "prepared":
            controls = _dispatch_controls(conn, config, actor, op)
            if any(item["state"] in {"queued", "running"} for item in controls.get("requests", [])):
                raise ControlError("发送队列正在处理；请等待队列状态更新后再操作。")
        _reserve_for_operation(conn, actor, request_id, argv, op)
        result = execute(conn, config, action="cancel-write", payload={
            "operation_id": op["operation_id"], "expected_digest": argv[3],
        })
        return {"command": "project_bug", "text": f"本地写入准备状态：{result['state']}。未执行远端写入。"}

    if len(argv) == 5 and argv[1] == "reconcile-write":
        op = _owned(conn, actor, argv[2])
        already_bound = check_intent(conn, config, argv, request_id)
        kind = "comment_reconcile" if op["action"] == "bug.comment" else "write_reconcile"
        prior = conn.execute(
            "SELECT activity_id,kind,bug_id,grant_id,source_json FROM project_activity_requests WHERE actor=? AND request_id=?",
            (actor, request_id),
        ).fetchone()
        if prior is not None:
            expected_source = {"operation_id": op["operation_id"], "write_digest": argv[3]}
            if (prior["kind"] != kind
                    or prior["bug_id"] != op["bug_id"] or prior["grant_id"] != argv[4]
                    or json.loads(prior["source_json"] or "null") != expected_source):
                _intent_conflict()
            if not already_bound:
                _reserve_for_operation(conn, actor, request_id, argv, op)
            from .project_activity import status as activity_status
            existing = activity_status(conn, actor=actor, activity_id=prior["activity_id"])
            return {"command": "project_bug", "text":
                    f"已有只读核对回执：{existing['activity_id']} · {existing['state']}。不会重发写入。"}
        if op["state"] not in {"dispatched", "unknown"} or not op["write_digest"] or argv[3] != op["write_digest"]:
            raise ControlError("仅可用匹配摘要核对已派发或待核对操作；不会重新发送。")
        action = "comment-reconcile" if op["action"] == "bug.comment" else "write-reconcile"
        if not already_bound:
            _reserve_for_operation(conn, actor, request_id, argv, op)
        result = execute(conn, config, action=action, payload={
            "operation_id": op["operation_id"], "write_digest": argv[3],
            "grant_id": argv[4], "request_id": request_id,
        })
        return {"command": "project_bug", "text":
                f"只读核对已进入现有队列：{result['activity_id']} · {result['state']}。不会重发写入。"}

    raise ControlError("用法：bug writes <Bug ID>；bug write <操作ID>；bug send-write <操作ID> <请求摘要>；bug cancel-write <操作ID> <请求摘要>；bug reconcile-write <操作ID> <写入摘要> <只读授权ID>")
